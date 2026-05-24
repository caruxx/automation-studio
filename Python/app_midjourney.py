#!/usr/bin/env python3
"""AceDataCloud Midjourney API への薄い REST クライアント。

セキュリティ設計 (SECURITY_NOTES.md §1.3):
  - token は引数または dashboard_config から取得、コード内 hardcode 禁止
  - ホスト名は api.acedata.cloud で固定 (DNS リバインディング対策)
  - エラー時に Authorization ヘッダを print/log しない
  - タイムアウト 300s、同時 1 リクエスト推奨

API 仕様 (MidjourneyCli クライアントから逆引き):
  POST https://api.acedata.cloud/midjourney/imagine
  Headers: Authorization: Bearer <token>, Content-Type: application/json
  Body: {
    "prompt": "...",
    "aspect_ratio": "16:9" | "1:1" | "9:16" | "4:3" | "3:4",
    "version": 8,           # 任意
    "mode": "fast" | "turbo" | "relax",  # 任意
    "callback_url": "https://api.acedata.cloud/health",  # 同期化のためのダミー
  }
  Response: { "task_id": "...", "image_url": "...", "status": "success" | "running" | ... }
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional


API_BASE_URL = "https://api.acedata.cloud"
API_ENDPOINT_IMAGINE = "/midjourney/imagine"
DEFAULT_TIMEOUT = 300
ASYNC_CALLBACK_URL = "https://api.acedata.cloud/health"


class MidjourneyError(Exception):
    """Midjourney 関連の汎用例外。"""
    def __init__(self, message: str, code: str = "", status_code: int = 0):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class MidjourneyAuthError(MidjourneyError):
    """認証失敗 (401/403)。"""
    pass


class MidjourneyQuotaError(MidjourneyError):
    """レート制限/クォータ超過 (429) や残高不足。"""
    pass


def _aspect_to_str(aspect: str) -> str:
    """gpt-image-2 と同じ表記を受け入れる。"""
    a = (aspect or "16:9").strip()
    if a not in ("16:9", "1:1", "9:16", "4:3", "3:4"):
        a = "16:9"
    return a


def _safe_token_preview(token: str) -> str:
    """ログ用に末尾だけ残してマスク。"""
    if not token:
        return "(empty)"
    return token[:4] + "***" + token[-4:] if len(token) >= 12 else "***"


def imagine_one(
    prompt: str,
    dest_path: Path,
    *,
    api_token: str,
    aspect_ratio: str = "16:9",
    version: int = 8,
    mode: str = "fast",
    timeout_sec: int = DEFAULT_TIMEOUT,
    reference_image_urls: Optional[list[str]] = None,
) -> dict:
    """1 プロンプトを imagine API に投げて画像を保存。

    Args:
        prompt: 生成プロンプト
        dest_path: 保存先パス
        api_token: AceDataCloud API token
        aspect_ratio: 16:9 / 1:1 / 9:16 / 4:3 / 3:4
        version: Midjourney バージョン (5/5.1/5.2/6/6.1/7/8)
        mode: fast / turbo / relax
        timeout_sec: HTTP タイムアウト秒
        reference_image_urls: 参照画像 URL (Midjourney は --cref などで参照画像対応)

    Returns:
        dict: {"ok": bool, "path": str|None, "error": str, "task_id": str, "image_url": str}
    """
    if not api_token:
        raise MidjourneyAuthError("AceDataCloud API token が未設定です")
    try:
        import httpx
    except ImportError as e:
        raise MidjourneyError("httpx パッケージが必要です") from e

    payload: dict = {
        "prompt": prompt,
        "aspect_ratio": _aspect_to_str(aspect_ratio),
        "version": int(version) if version else 8,
        "mode": mode if mode in ("fast", "turbo", "relax") else "fast",
        "callback_url": ASYNC_CALLBACK_URL,
    }
    if reference_image_urls:
        # Midjourney の --cref / --sref 系: 参照画像 URL のリスト
        # AceDataCloud がどう受け取るかは仕様次第なので image_urls キーで送る
        payload["image_urls"] = list(reference_image_urls)

    url = f"{API_BASE_URL}{API_ENDPOINT_IMAGINE}"
    headers = {
        "accept": "application/json",
        "authorization": f"Bearer {api_token}",
        "content-type": "application/json",
    }

    started = time.time()
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_sec)) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as e:
        raise MidjourneyError(f"タイムアウト ({timeout_sec}s)") from e
    except httpx.RequestError as e:
        # ネットワーク層エラー (DNS 失敗等)。Authorization は __str__ に含まれないので安全
        raise MidjourneyError(f"通信エラー: {type(e).__name__}") from e

    if resp.status_code == 401:
        raise MidjourneyAuthError("AceDataCloud API token が無効 (401)")
    if resp.status_code == 403:
        raise MidjourneyAuthError("AceDataCloud API 権限不足 (403)")
    if resp.status_code == 429:
        raise MidjourneyQuotaError("AceDataCloud レート制限到達 (429)")
    if resp.status_code >= 400:
        # 詳細は response.text に含まれるが、token を間違って晒さないよう先頭 500 文字に制限
        body = (resp.text or "")[:500]
        raise MidjourneyError(f"API エラー HTTP {resp.status_code}: {body}",
                              code=f"http_{resp.status_code}",
                              status_code=resp.status_code)

    try:
        data = resp.json()
    except ValueError:
        raise MidjourneyError(f"レスポンスが JSON ではありません: {(resp.text or '')[:200]}")

    # 想定レスポンス: {"task_id": "...", "image_url": "...", "status": "..."}
    image_url = (
        data.get("image_url")
        or data.get("imageUrl")
        or (data.get("data") or {}).get("image_url")
        or (data.get("response") or {}).get("image_url")
    )
    task_id = data.get("task_id") or data.get("taskId") or ""
    status = (data.get("status") or "").lower()

    if not image_url:
        # 非同期で完了していない可能性 (status=processing 等)。task_id でポーリングが必要だが、
        # callback_url で同期化を試みているのでここに来たら異常終了として扱う
        raise MidjourneyError(
            f"画像 URL が返ってきませんでした (task_id={task_id}, status={status}, "
            f"keys={list(data.keys())[:6]})"
        )

    # 画像ダウンロード
    try:
        with httpx.Client(timeout=httpx.Timeout(60)) as dl:
            r = dl.get(image_url)
            r.raise_for_status()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(r.content)
    except Exception as e:
        raise MidjourneyError(f"画像ダウンロード失敗 ({image_url[:80]}): {type(e).__name__}") from e

    elapsed = int(time.time() - started)
    return {
        "ok": True,
        "path": str(dest_path),
        "task_id": task_id,
        "image_url": image_url,
        "elapsed": elapsed,
        "size_kb": dest_path.stat().st_size // 1024,
    }


def imagine_batch(
    prompts: list[tuple[str, str]],
    output_dir: Path,
    *,
    api_token: str,
    aspect_ratio: str = "16:9",
    version: int = 8,
    mode: str = "fast",
    timeout_sec: int = DEFAULT_TIMEOUT,
    reference_image_urls: Optional[list[str]] = None,
    log_fn=None,
) -> list[dict]:
    """N 件のプロンプトを順次 imagine 実行。

    Args:
        prompts: [(prompt, filename), ...]
        output_dir: 保存先ディレクトリ
        api_token: token
        log_fn: ログ出力 callable(str)。None なら print。

    Returns:
        各プロンプトの結果 list
    """
    log = log_fn or print
    log(f"Midjourney API: {len(prompts)} 件を順次生成 (mode={mode}, aspect={aspect_ratio}, "
        f"token={_safe_token_preview(api_token)})")
    results: list[dict] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    for i, (prompt, filename) in enumerate(prompts, 1):
        dest = output_dir / filename
        # 衝突回避
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix or ".png"
            k = 2
            while True:
                cand = output_dir / f"{stem}-{k}{suffix}"
                if not cand.exists():
                    log(f"  ⚠ 既存衝突回避: {filename} → {cand.name}")
                    dest = cand
                    filename = dest.name
                    break
                k += 1
                if k > 999:
                    break
        log(f"[{i}/{len(prompts)}] {filename} ← {prompt[:60]}")
        try:
            r = imagine_one(
                prompt, dest,
                api_token=api_token,
                aspect_ratio=aspect_ratio,
                version=version,
                mode=mode,
                timeout_sec=timeout_sec,
                reference_image_urls=reference_image_urls,
            )
            log(f"  ✓ {filename} ({r['elapsed']}s, {r['size_kb']}KB)")
            results.append({**r, "filename": filename, "prompt": prompt})
        except MidjourneyAuthError as e:
            log(f"  ✗ 認証失敗: {e}")
            results.append({"ok": False, "filename": filename, "prompt": prompt,
                            "error": str(e), "error_kind": "auth"})
            break  # 認証失敗は全件失敗するのでループ中断
        except MidjourneyQuotaError as e:
            log(f"  ✗ クォータ超過: {e}")
            results.append({"ok": False, "filename": filename, "prompt": prompt,
                            "error": str(e), "error_kind": "quota"})
            break  # クォータも中断
        except MidjourneyError as e:
            log(f"  ✗ エラー: {e}")
            results.append({"ok": False, "filename": filename, "prompt": prompt,
                            "error": str(e), "error_kind": "api"})
        except Exception as e:
            log(f"  ✗ 例外 {type(e).__name__}: {e}")
            results.append({"ok": False, "filename": filename, "prompt": prompt,
                            "error": f"{type(e).__name__}: {e}", "error_kind": "unknown"})
    ok = sum(1 for r in results if r.get("ok"))
    log(f"[DONE] 成功 {ok}/{len(results)}")
    return results


def test_token(api_token: str, timeout_sec: int = 15) -> dict:
    """トークンの疎通テスト。imagine を最低構成で呼んで認証/権限を確認。"""
    if not api_token:
        return {"ok": False, "error": "token が未設定"}
    try:
        import httpx
    except ImportError as e:
        return {"ok": False, "error": "httpx not installed"}
    url = f"{API_BASE_URL}/midjourney/translate"   # 軽量で課金されにくいエンドポイント
    try:
        with httpx.Client(timeout=httpx.Timeout(timeout_sec)) as client:
            resp = client.post(url, json={"prompt": "test", "target": "en"},
                                headers={
                                    "authorization": f"Bearer {api_token}",
                                    "content-type": "application/json",
                                })
        if resp.status_code == 401:
            return {"ok": False, "error": "Token が無効 (401)"}
        if resp.status_code == 403:
            return {"ok": False, "error": "権限不足 (403)"}
        return {"ok": True, "status_code": resp.status_code,
                "token_preview": _safe_token_preview(api_token)}
    except httpx.TimeoutException:
        return {"ok": False, "error": f"タイムアウト ({timeout_sec}s)"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}"}
