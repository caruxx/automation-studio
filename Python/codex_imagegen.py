#!/usr/bin/env python3
"""OpenAI Image API / Codex CLI を使った画像生成（並列実行）。

Image2 は Flow と違って画面上に参照画像アップロード欄がないため、
参照画像がある場合は OpenAI Image API の edits/reference-image 経路を使う。
OPENAI_API_KEY が無い・参照画像も無い場合だけ Codex CLI にフォールバックする。

CLI 例:
    python3 codex_imagegen.py --output-dir /path/to/Image --max-parallel 5 \\
        --prompts-file prompts.txt
    python3 codex_imagegen.py --output-dir /path/to/Image --prompt "夜の名古屋::nagoya"
    python3 codex_imagegen.py --output-dir /path/to/Image --reference-image ref.jpg \\
        --size 1536x1024 --quality medium --prompt "Subject: ..."
"""
from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

CODEX_APP_FALLBACK = "/Applications/Codex.app/Contents/Resources/codex"
OPENAI_IMAGE_GENERATIONS_URL = "https://api.openai.com/v1/images/generations"
OPENAI_IMAGE_EDITS_URL = "https://api.openai.com/v1/images/edits"


def find_codex_cli() -> str:
    p = shutil.which("codex")
    if p:
        return p
    if Path(CODEX_APP_FALLBACK).exists():
        return CODEX_APP_FALLBACK
    raise FileNotFoundError(
        "codex CLI が見つかりません。Codex.app をインストールするか、codex を PATH に追加してください。"
    )


_SLUG_RE = re.compile(r"[^\w぀-ゟ゠-ヿ一-鿿-]+", re.UNICODE)


def slugify(text: str, max_len: int = 30) -> str:
    s = text.strip().replace(" ", "-")
    s = _SLUG_RE.sub("-", s).strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "image"


def parse_prompt_lines(lines: list[str]) -> list[tuple[str, str]]:
    """テキスト入力を「1 ブロック = 1 プロンプト」として解釈する。

    - ブロックは **空行 1 行以上で区切る**（5要素プロンプトのような複数行プロンプトに対応）。
    - 各ブロック内の改行はそのまま保持され、Subject: / Background: / ... のラベル付き
      構造化プロンプトをそのまま OpenAI API に送れる。
    - `::filename` はブロックの **最終行末尾** に書く（または書かない場合は先頭行からスラッグ化）。
    - `#` で始まる行はコメントとして無視。
    """
    # 1) 行を集めて空行でブロック分割
    blocks: list[list[str]] = []
    cur: list[str] = []
    for raw in lines:
        s = raw.rstrip("\r\n")
        if not s.strip():
            if cur:
                blocks.append(cur)
                cur = []
            continue
        if s.strip().startswith("#"):
            # コメント行はそのブロックに含めない（ブロック先頭・途中問わず無視）
            continue
        cur.append(s)
    if cur:
        blocks.append(cur)

    # 2) 各ブロックを (prompt, filename) に
    out: list[tuple[str, str]] = []
    used: set[str] = set()
    for block in blocks:
        if not block:
            continue
        # ::filename は最終行末尾
        last = block[-1]
        if "::" in last:
            head, _, fname = last.rpartition("::")
            block[-1] = head.rstrip()
            fname = fname.strip()
        else:
            fname = ""
        prompt = "\n".join(ln for ln in block if ln.strip()).strip()
        if not prompt:
            continue
        if not fname:
            # 先頭行（Subject: 行など）の右辺だけからスラッグ生成
            first_line = prompt.split("\n", 1)[0]
            seed = first_line.split(":", 1)[-1].strip() if ":" in first_line else first_line
            fname = slugify(seed)
        # 拡張子付与
        if "." not in fname or fname.rsplit(".", 1)[-1].lower() not in ("png", "jpeg", "jpg", "webp"):
            fname += ".png"
        # 重複回避
        base, ext = fname.rsplit(".", 1)
        cand = fname
        i = 2
        while cand in used:
            cand = f"{base}-{i}.{ext}"
            i += 1
        used.add(cand)
        out.append((prompt, cand))
    return out


_log_lock = threading.Lock()


def _log(msg: str) -> None:
    with _log_lock:
        print(msg, flush=True)


def _build_codex_command(
    codex_cli: str,
    prompt: str,
    dest_path: Path,
    *,
    model: str = "gpt-image-2",
    size: str = "1536x1024",
    quality: str = "medium",
    output_format: str = "png",
    reference_images: list[Path] | None = None,
) -> list[str]:
    refs = [p for p in (reference_images or []) if p.exists()]
    ref_block = ""
    if refs:
        listed = "\n".join(f"  - {p}" for p in refs)
        ref_block = (
            "\n## 参照画像（必ず最初に読み込んで視覚的に分析すること）\n"
            f"{listed}\n\n"
            "## 参照画像からの再構成ルール（厳守）\n"
            "1. 必ず先に参照画像群を分析してください。"
            "被写体配置 / 構図 / ライティング / 色温度 / 視線誘導 / "
            "「クリックを誘う注目ポイント」を言語化してください。\n"
            "2. その分析結果から、以下の 3 点だけを保持してください:\n"
            "   - 構成要素（被写体の配置・サイズバランス・構図）\n"
            "   - ライティング（光源方向・色温度・コントラスト）\n"
            "   - あなたが「視聴者の注目を引く」と判断したポイント\n"
            "3. それ以外の要素（人物の顔・固有のロゴ・商標・キャラクター・チャンネル名・"
            "特徴的な小道具・テロップ）は「訴えられない程度」=「元画像と同一視されない程度」"
            "に変形・差し替え・抽象化して再構成してください。\n"
            "4. 「コピー」ではなく「翻訳」を行ってください。元画像の固有要素を残してはいけません。\n"
            "5. 参照画像が複数ある場合は、それぞれの「効いている要素」だけを抽出して、"
            "下記の生成プロンプト本体と統合してください。\n"
        )
    instruction = (
        f"OpenAI の {model} 相当の画像生成として、次のプロンプトで画像を生成して。\n"
        f"推奨設定: size={size}, quality={quality}, output_format={output_format}。\n"
        f"{ref_block}"
        f"\n## 生成プロンプト本体\n{prompt}\n\n"
        "生成できたら、~/.codex/generated_images/ "
        f"以下で最も新しいファイルを {dest_path} にコピーして。"
        "コピー先のファイルパスだけを出力して。"
    )
    return [
        codex_cli,
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        instruction,
    ]


def _api_headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _response_image_bytes_list(payload: dict) -> list[bytes]:
    data = payload.get("data") or []
    if not data:
        raise RuntimeError(f"画像データが空です: {str(payload)[:300]}")
    out: list[bytes] = []
    for item in data:
        b64 = item.get("b64_json")
        if not b64:
            continue
        out.append(base64.b64decode(b64))
    if not out:
        raise RuntimeError(f"b64_json がありません: {str(data[0])[:300]}")
    return out


# 16:9 / 1:1 / 9:16 / 4:3 / 3:4 → gpt-image-2 でサポートされる標準サイズ
_ASPECT_TO_SIZE = {
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "1:1":  "1024x1024",
    "4:3":  "1536x1024",   # gpt-image-2 は 4:3 標準なしで近い 1536x1024 に倒す
    "3:4":  "1024x1536",
}


def aspect_to_size(aspect: str, fallback: str = "1536x1024") -> str:
    return _ASPECT_TO_SIZE.get((aspect or "").strip(), fallback)


def _generate_with_openai_api(
    prompt: str,
    dest_path: Path,
    *,
    reference_images: list[Path] | None = None,
    model: str = "gpt-image-2",
    size: str = "1536x1024",
    quality: str = "medium",
    output_format: str = "png",
    background: str = "auto",
    moderation: str = "auto",
    input_fidelity: str = "high",
    n: int = 1,
    timeout_sec: int = 900,
) -> dict:
    try:
        import httpx
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("httpx が見つかりません。pyproject.toml の依存関係をインストールしてください") from e

    api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY が未設定です。Image2 の参照画像生成は API 経路が必要です")

    refs = [p for p in (reference_images or []) if p.exists()]
    n_clamped = max(1, min(int(n or 1), 10))
    data: dict = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "quality": quality,
        "output_format": output_format,
        "n": n_clamped,
    }
    # gpt-image-2 のみ受け付けるフィールド (auto は API 側既定なので送らないことで互換性向上)
    if background and background != "auto":
        data["background"] = background
    if moderation and moderation != "auto":
        data["moderation"] = moderation
    timeout = httpx.Timeout(timeout_sec)
    if refs:
        if input_fidelity in ("high", "low"):
            data["input_fidelity"] = input_fidelity
        handles = []
        files = []
        try:
            for p in refs:
                mime = mimetypes.guess_type(p.name)[0] or "image/png"
                fh = p.open("rb")
                handles.append(fh)
                files.append(("image[]", (p.name, fh, mime)))
            resp = httpx.post(
                OPENAI_IMAGE_EDITS_URL,
                headers=_api_headers(api_key),
                data=data,
                files=files,
                timeout=timeout,
            )
        finally:
            for fh in handles:
                try:
                    fh.close()
                except Exception:
                    pass
    else:
        resp = httpx.post(
            OPENAI_IMAGE_GENERATIONS_URL,
            headers=_api_headers(api_key),
            json=data,
            timeout=timeout,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI Image API error {resp.status_code}: {resp.text[:500]}")
    images = _response_image_bytes_list(resp.json())
    saved: list[str] = []
    if len(images) == 1:
        dest_path.write_bytes(images[0])
        saved.append(str(dest_path))
    else:
        stem = dest_path.stem
        suffix = dest_path.suffix or ".png"
        parent = dest_path.parent
        for idx, blob in enumerate(images, start=1):
            p = parent / f"{stem}-{idx}{suffix}"
            p.write_bytes(blob)
            saved.append(str(p))
    return {"backend": "openai_api", "reference_count": len(refs),
            "n": n_clamped, "saved_paths": saved}


def generate_one(codex_cli: str | None, prompt: str, filename: str, output_dir: Path,
                 timeout_sec: int = 900, reference_images: list[Path] | None = None,
                 model: str = "gpt-image-2", size: str = "1536x1024",
                 quality: str = "medium", output_format: str = "png",
                 backend: Literal["auto", "api", "codex"] = "auto",
                 background: str = "auto", moderation: str = "auto",
                 input_fidelity: str = "high", n: int = 1) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / filename
    # 衝突回避（安全網）: 既存ファイルがあれば -2, -3, ... を付与
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix or ".png"
        k = 2
        while True:
            cand = output_dir / f"{stem}-{k}{suffix}"
            if not cand.exists():
                _log(f"  ⚠ 既存ファイル衝突回避: {filename} → {cand.name}")
                dest = cand
                filename = dest.name
                break
            k += 1
            if k > 999:
                break
    refs = [p for p in (reference_images or []) if p.exists()]
    use_api = backend == "api" or (
        backend == "auto" and bool(os.environ.get("OPENAI_API_KEY")) and (refs or model.startswith("gpt-image"))
    )
    started = time.time()
    _log(f"[START] {filename} ← {prompt[:60]}{'…' if len(prompt) > 60 else ''}")
    if use_api:
        try:
            meta = _generate_with_openai_api(
                prompt, dest,
                reference_images=refs,
                model=model,
                size=size,
                quality=quality,
                output_format=output_format,
                background=background,
                moderation=moderation,
                input_fidelity=input_fidelity,
                n=n,
                timeout_sec=timeout_sec,
            )
        except Exception as e:  # noqa: BLE001
            _log(f"[ERR] {filename} API: {e}")
            return {"ok": False, "filename": filename, "prompt": prompt,
                    "error": str(e), "path": None}
        elapsed = int(time.time() - started)
        saved = meta.get("saved_paths") or [str(dest)]
        head = Path(saved[0])
        try:
            head_size = head.stat().st_size // 1024
        except Exception:
            head_size = 0
        suffix = f" + {len(saved)-1} more" if len(saved) > 1 else ""
        _log(f"[OK] {filename} ({elapsed}s, {head_size}KB{suffix}, {meta['backend']})")
        return {"ok": True, "filename": filename, "prompt": prompt,
                "path": saved[0], "paths": saved, "elapsed": elapsed, **meta}

    # Codex CLI 経路 — ChatGPT サブスク経由で Codex のエージェントが
    # Python で画像生成スクリプトを書いて実行する流れ（過去実績あり）
    if not codex_cli:
        codex_cli = find_codex_cli()
    if refs:
        _log(f"  参照画像 {len(refs)} 枚を Codex CLI 経路で分析 → 再構成指示で渡します")
    cmd = _build_codex_command(
        codex_cli, prompt, dest,
        model=model,
        size=size,
        quality=quality,
        output_format=output_format,
        reference_images=refs,
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(Path.home() / "Desktop"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        _log(f"[TIMEOUT] {filename} ({timeout_sec}s)")
        return {"ok": False, "filename": filename, "prompt": prompt,
                "error": f"timeout after {timeout_sec}s", "path": None}
    elapsed = int(time.time() - started)
    out_text = (proc.stdout or "").strip()
    if dest.exists() and dest.stat().st_size > 0:
        _log(f"[OK] {filename} ({elapsed}s, {dest.stat().st_size // 1024}KB)")
        return {"ok": True, "filename": filename, "prompt": prompt,
                "path": str(dest), "elapsed": elapsed}
    # 失敗時は標準出力末尾を診断材料として返す
    tail = "\n".join(out_text.splitlines()[-15:]) if out_text else ""

    # 既知のエラーパターン → 明確なメッセージに翻訳
    error_kind = "unknown"
    if "usage limit" in out_text.lower() or "hit your usage limit" in out_text.lower():
        error_kind = "codex_usage_limit"
        import re as _re
        m = _re.search(r"try again at ([A-Za-z0-9 :,.\-]+\d{4}[^.\n]*)", out_text, _re.IGNORECASE)
        retry_at = m.group(1).strip() if m else "(時刻不明)"
        explicit = (
            f"Codex CLI (ChatGPT サブスクリプション) の使用量上限に到達しています。"
            f"回復時刻: {retry_at}。当面は右パネルのプロバイダーで Flow / Nano Banana 2 に切り替えてください。"
            f"または ChatGPT Pro へのアップグレード、OpenAI API キーの取得で回避できます。"
        )
        _log(f"[QUOTA] {filename}: {explicit}")
        return {"ok": False, "filename": filename, "prompt": prompt,
                "error": explicit, "error_kind": error_kind,
                "diagnostic_tail": tail, "path": None}
    if "x-codex-turn-metadata" in out_text or "UTF-8 encoding error" in out_text:
        error_kind = "codex_header_encoding"
        explicit = (
            "Codex CLI の WebSocket 接続エラー (HTTP ヘッダに非 ASCII 文字が含まれる)。"
            "ワークスペースパスに日本語が含まれていることが原因の可能性があります。"
        )
        _log(f"[ENC] {filename}: {explicit}")
        return {"ok": False, "filename": filename, "prompt": prompt,
                "error": explicit, "error_kind": error_kind,
                "diagnostic_tail": tail, "path": None}

    _log(f"[ERR] {filename} (rc={proc.returncode}, {elapsed}s)\n{tail}")
    return {"ok": False, "filename": filename, "prompt": prompt,
            "error": tail or f"exit={proc.returncode}",
            "error_kind": error_kind, "diagnostic_tail": tail, "path": None}


def run(prompts: list[tuple[str, str]], output_dir: Path,
        max_parallel: int = 5, timeout_sec: int = 900,
        reference_images: list[Path] | None = None,
        model: str = "gpt-image-2", size: str = "1536x1024",
        quality: str = "medium", output_format: str = "png",
        backend: Literal["auto", "api", "codex"] = "auto",
        background: str = "auto", moderation: str = "auto",
        input_fidelity: str = "high", n: int = 1) -> list[dict]:
    if not prompts:
        _log("[WARN] プロンプトが空です")
        return []
    refs = [p for p in (reference_images or []) if p.exists()]
    has_api_key = bool(os.environ.get("OPENAI_API_KEY"))
    # backend=codex か、auto で API キー無しなら Codex CLI を使う（refs があっても新仕様で受け入れる）
    should_use_codex = backend == "codex" or (backend == "auto" and not has_api_key)
    codex_cli = find_codex_cli() if should_use_codex else None
    if codex_cli:
        _log(f"codex CLI: {codex_cli}")
    else:
        _log(f"backend: OpenAI Image API  model={model} size={size} quality={quality} n={n}")
    _log(f"出力先: {output_dir}")
    _log(f"プロンプト数: {len(prompts)} / 並列上限: {max_parallel}")
    if refs:
        _log("参照画像: " + ", ".join(p.name for p in refs) + f"  (input_fidelity={input_fidelity})")
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, min(max_parallel, len(prompts)))) as ex:
        futs = {
            ex.submit(
                generate_one, codex_cli, p, fn, output_dir, timeout_sec,
                reference_images, model, size, quality, output_format, backend,
                background, moderation, input_fidelity, n,
            ): (p, fn)
            for p, fn in prompts
        }
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:  # noqa: BLE001
                p, fn = futs[fut]
                _log(f"[CRASH] {fn}: {e}")
                results.append({"ok": False, "filename": fn, "prompt": p,
                                "error": str(e), "path": None})
    ok = sum(1 for r in results if r["ok"])
    total_files = sum(len(r.get("paths") or [r.get("path")]) if r.get("ok") else 0 for r in results)
    _log(f"[DONE] 成功 {ok}/{len(results)}  生成枚数: {total_files}")
    for r in results:
        mark = "✓" if r["ok"] else "✗"
        if r["ok"]:
            paths = r.get("paths") or [r.get("path")]
            for pth in paths:
                _log(f"  {mark} {pth}")
        else:
            _log(f"  {mark} {r['filename']} : {r.get('error','')[:200]}")
    return results


def _read_prompts(args: argparse.Namespace) -> list[str]:
    lines: list[str] = []
    if args.prompts_file:
        text = Path(args.prompts_file).read_text(encoding="utf-8")
        lines.extend(text.splitlines())
    if args.prompt:
        lines.extend(args.prompt)
    if not lines and not sys.stdin.isatty():
        lines.extend(sys.stdin.read().splitlines())
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="ChatGPT/Codex CLI 経由の画像生成（並列）")
    ap.add_argument("--output-dir", required=True, help="保存先ディレクトリ")
    ap.add_argument("--prompt", action="append", default=[],
                    help="プロンプト（複数指定可、`name::filename` 形式対応）")
    ap.add_argument("--prompts-file", help="プロンプトを 1 行 1 件で書いたファイル")
    ap.add_argument("--max-parallel", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=900, help="1 件あたりのタイムアウト秒")
    ap.add_argument("--reference-image", action="append", default=[],
                    help="参照画像パス（複数指定可）。抽象要素だけを再構成する指示に使う")
    ap.add_argument("--model", default="gpt-image-2", help="画像生成モデル名の指示")
    ap.add_argument("--size", default="1536x1024", help="画像サイズの指示")
    ap.add_argument("--quality", default="medium", choices=["low", "medium", "high", "auto"],
                    help="品質。low=ドラフト, medium/high=最終, auto=API 任せ")
    ap.add_argument("--output-format", default="png", choices=["png", "jpeg", "webp"],
                    help="出力形式の指示")
    ap.add_argument("--backend", default="auto", choices=["auto", "api", "codex"],
                    help="auto: OPENAI_API_KEY があれば Image API、なければ Codex CLI。参照画像ありは API 必須")
    ap.add_argument("--background", default="auto", choices=["auto", "transparent", "opaque"],
                    help="背景の指定。auto は API 既定（gpt-image-2）")
    ap.add_argument("--moderation", default="auto", choices=["auto", "low"],
                    help="モデレーション。low は creative 寄り（gpt-image-2）")
    ap.add_argument("--input-fidelity", default="high", choices=["high", "low"],
                    help="参照画像の忠実度（edits）。high は元画像に近づく")
    ap.add_argument("--n", type=int, default=1,
                    help="1 リクエストあたりの生成枚数 (1-10)。複数の場合は filename-1.png, -2.png 形式")
    ap.add_argument("--aspect", default="",
                    help="アスペクト比 (16:9 / 9:16 / 1:1 / 4:3 / 3:4)。指定時は --size より優先して標準サイズへ変換")
    args = ap.parse_args()

    size = args.size
    if args.aspect:
        size = aspect_to_size(args.aspect, fallback=size)

    raw = _read_prompts(args)
    parsed = parse_prompt_lines(raw)
    if not parsed:
        print("プロンプトが指定されていません（--prompt / --prompts-file / stdin）", file=sys.stderr)
        return 2
    out = Path(args.output_dir).expanduser().resolve()
    refs = [Path(p).expanduser().resolve() for p in args.reference_image]
    results = run(
        parsed, out,
        max_parallel=args.max_parallel,
        timeout_sec=args.timeout,
        reference_images=refs,
        model=args.model,
        size=size,
        quality=args.quality,
        output_format=args.output_format,
        backend=args.backend,
        background=args.background,
        moderation=args.moderation,
        input_fidelity=args.input_fidelity,
        n=args.n,
    )
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
