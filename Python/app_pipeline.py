#!/usr/bin/env python3
"""orzz. 一括パイプライン
================================

「vol.78 を全工程やって」の一言で 6 工程を順に実行する統合スクリプト。
各工程は独立しており、途中から再開もできる。

使い方:
  # 全工程実行（SUNO 生成 → リネーム → Premiere → 書き出し → メタ生成 → アップロード）
  python3 app_pipeline.py 78

  # 特定工程から開始
  python3 app_pipeline.py 78 --from premiere

  # 特定工程だけ実行
  python3 app_pipeline.py 78 --only meta

  # ドライラン（何が実行されるか確認）
  python3 app_pipeline.py 78 --dry-run

  # Web サーバー経由（localhost:8888 が起動中なら API を叩く）
  python3 app_pipeline.py 78 --via-api

前提:
  - Web サーバー (localhost:8888) が起動中（--via-api 時）
  - または各スクリプトが直接実行可能（デフォルト）
  - Claude CLI が PATH 上にあること
  - Premiere Pro が起動していること（premiere / export 工程時）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# ─── 設定 ───
BASE = Path(__file__).resolve().parent
HOME = Path.home()
# 設定ディレクトリ（v2 配布化対応・共通モジュール経由）
try:
    sys.path.insert(0, str(Path(__file__).parent))
    from _app_config import resolve_config_dir as _resolve_config_dir
    CONFIG_DIR = _resolve_config_dir()
except Exception:
    CONFIG_DIR = HOME / ".config" / "orzz"
API_BASE = "http://localhost:8888"

# 子プロセスがブラウザ手動ログインを要求した場合の sentinel exit code。
# suno_auto_create.py の UnattendedLoginRequired ハンドラと一致させる。
EXIT_UNATTENDED = 75
# 子プロセスが「transient / retryable な失敗」を示す場合の sentinel exit code。
# app_youtube.py の HttpError ハンドラと一致させる（403 quotaExceeded / 429 / 5xx）。
EXIT_RETRYABLE = 76
# 子プロセスが「YouTube クオータ枯渇（24h ウィンドウで上限到達）」を示す sentinel。
# 短時間の retry では復旧しないので、即座に Discord 通知して停止する。
EXIT_QUOTA_EXHAUSTED = 77
# preflight チェック（Premiere Pro / CEP パネルが未起動など）に失敗した場合の sentinel。
# pipeline 開始前に判明するので、各 step を一切実行せず早期終了する。
EXIT_PREFLIGHT_FAIL = 78

STEPS = ["suno", "rename", "bgimage", "psd_composite", "premiere", "export", "qa", "meta", "localization", "thumbnail", "upload"]
STEPS_WITH_PLAN = ["plan", "suno", "rename", "bgimage", "psd_composite", "premiere", "export", "qa", "meta", "localization", "thumbnail", "upload"]
STEP_LABELS = {
    "plan":          "0/11 ベンチマーク分析 → 次動画プラン生成",
    "suno":          "1/11 SUNO 楽曲生成",
    "rename":        "2/11 楽曲リネーム + 音声処理",
    "bgimage":       "3/11 背景画像生成（ベンチマーク参照 + チャンネルコンセプト）",
    "psd_composite": "4/11 PSD 合成（vol{N}.jpg + サムネイル.jpg を 2 枚出し）",
    "premiere":      "5/11 Premiere 自動配置",
    "export":        "6/11 書き出し",
    "qa":            "7/11 QA チェック（解像度 / アスペクト / 尺 / コーデック）",
    "meta":          "8/11 動画メタ（タイトル・説明・タグ）",
    "localization":  "9/11 多言語メタデータ（メイン言語 → 各国翻訳）",
    "thumbnail":     "10/11 AI サムネイル（PSD 合成失敗時のフォールバック）",
    "upload":        "11/11 YouTube アップロード",
}


_CHANNEL_CONFIG_FILENAME = ".app_channel_config.json"
_CHANNELS_REGISTRY = CONFIG_DIR / "channels.json"


def _load_channels_registry() -> list:
    """`~/.config/{app_id}/channels.json` を canonical source として読む。

    P2-2 で「channels.json を first-class 化」した結果、pipeline / 他 CLI は
    channel_id <-> folder の双方向解決にこれを使う。"""
    if not _CHANNELS_REGISTRY.exists():
        return []
    try:
        d = json.loads(_CHANNELS_REGISTRY.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _resolve_channel(*, channel_id: str = "", channel_folder: str = "") -> dict:
    """channel_id か folder から registry エントリを返す（双方向解決）。
    どちらも空なら {} を返す。"""
    if not channel_id and not channel_folder:
        return {}
    chs = _load_channels_registry()
    if channel_id:
        for ch in chs:
            if ch.get("id") == channel_id:
                return ch
        return {}
    # folder マッチ（パス文字列の正規化込み）
    target = str(Path(channel_folder).expanduser().resolve())
    for ch in chs:
        try:
            if str(Path(ch.get("folder") or "").expanduser().resolve()) == target:
                return ch
        except Exception:
            continue
    return {}


def _load_dashboard_config():
    """ダッシュボード設定をロード。

    環境変数 `APP_CHANNEL_FOLDER` が設定されていれば、UI のアクティブチャンネル
    （グローバル `dashboard_config.json` の `channel_folder`）を**無視**し、
    指定フォルダを per-channel 設定の出所とする。これにより複数チャンネルの
    並列ジョブが同一プロセスツリーで衝突しなくなる。

    マージ順:
      1. グローバル `dashboard_config.json`（GLOBAL_ONLY_KEYS: brand_*, file_prefix, app_id, *_api_key 等）
      2. `<channel_folder>/.app_channel_config.json`（per-channel: persona, rival_channels, ...）
      3. 環境変数（最優先）: APP_CHANNEL_FOLDER, APP_CHANNEL_NAME
    """
    cfg = {}
    p = CONFIG_DIR / "dashboard_config.json"
    if p.exists():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}

    override_folder = (os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
    channel_folder = override_folder or (cfg.get("channel_folder") or "").strip()
    if channel_folder:
        ch_dir = Path(channel_folder).expanduser()
        per_channel_path = ch_dir / _CHANNEL_CONFIG_FILENAME
        if per_channel_path.exists():
            try:
                pc = json.loads(per_channel_path.read_text(encoding="utf-8")) or {}
                if isinstance(pc, dict):
                    cfg.update(pc)
            except Exception:
                pass
        cfg["channel_folder"] = str(ch_dir)
        env_name = (os.environ.get("APP_CHANNEL_NAME") or "").strip()
        if env_name:
            cfg["channel_name"] = env_name
        elif override_folder and "channel_name" not in cfg:
            cfg["channel_name"] = ch_dir.name
    return cfg


def _load_suno_config():
    p = CONFIG_DIR / "suno_config.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _load_channel_suno_config() -> dict:
    """per-channel `.app_channel_config.json` の `suno` ブロックを返す（無ければ {}）。

    解決順:
      1. APP_CHANNEL_FOLDER env （pipeline が --channel-id 解決時に立てる）
      2. グローバル `dashboard_config.json` の `channel_folder`（UI アクティブチャンネル）

    `_load_dashboard_config()` は per-channel 全体を flat merge してしまうため、
    channel config の `suno` をそのままネスト dict として参照したい場合はこちらを使う。
    """
    folder = (os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
    if not folder:
        p = CONFIG_DIR / "dashboard_config.json"
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8")) or {}
                folder = (d.get("channel_folder") or "").strip()
            except Exception:
                pass
    if not folder:
        return {}
    pc = Path(folder).expanduser() / _CHANNEL_CONFIG_FILENAME
    if not pc.exists():
        return {}
    try:
        d = json.loads(pc.read_text(encoding="utf-8")) or {}
        suno = d.get("suno")
        return suno if isinstance(suno, dict) else {}
    except Exception:
        return {}


def _resolve_folder(vol_num: int) -> Path | None:
    cfg = _load_dashboard_config()
    ch_dir = Path(cfg.get("channel_folder", ""))
    if not ch_dir.exists():
        return None
    prefix = str(cfg.get("file_prefix") or "vol").strip()
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "", prefix) or "vol"
    pat = re.compile(rf"^({vol_num})_(?:{re.escape(prefix)}|orzz)(?:_|$)")
    for d in ch_dir.iterdir():
        if d.is_dir() and pat.match(d.name):
            return d
    # 最後の保険: prefix が想定外でも vol 番号が一致する既存フォルダを拾う
    for d in ch_dir.iterdir():
        if not d.is_dir():
            continue
        m = re.match(r"^(\d+)_", d.name)
        if m and int(m.group(1)) == vol_num:
            return d
    return None


def _run(cmd, label, timeout=None, env_overrides=None):
    """サブコマンド実行ヘルパー。

    返り値:
      True               — 成功
      False              — 通常失敗
      "unattended_login" — exit code 75（子プロセスがブラウザ手動ログインを要求）

    env_overrides: 親 os.environ に上書きしたい環境変数 dict（指定時のみ env を明示）
    """
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  cmd: {' '.join(str(c) for c in cmd)}")
    print(f"{'='*60}\n")
    run_kwargs = {"timeout": timeout}
    if env_overrides:
        run_kwargs["env"] = {**os.environ, **env_overrides}
    try:
        proc = subprocess.run(cmd, **run_kwargs)
        if proc.returncode == EXIT_UNATTENDED:
            print(f"\n🔐 {label} 中断: ブラウザ手動ログインが必要です (exit={proc.returncode})")
            return "unattended_login"
        if proc.returncode == EXIT_RETRYABLE:
            print(f"\n🔁 {label} 一時失敗: retry 対象 (exit={proc.returncode})")
            return "retryable"
        if proc.returncode == EXIT_QUOTA_EXHAUSTED:
            print(f"\n📊 {label} 中断: YouTube クオータ枯渇 (exit={proc.returncode})")
            return "quota_exhausted"
        if proc.returncode != 0:
            print(f"\n❌ {label} 失敗 (exit={proc.returncode})")
            return False
        print(f"\n✅ {label} 完了")
        return True
    except subprocess.TimeoutExpired:
        suffix = f" ({timeout}s)" if timeout else ""
        print(f"\n⏰ {label} タイムアウト{suffix}")
        return False
    except Exception as e:
        print(f"\n❌ {label} エラー: {e}")
        return False


def _preflight_premiere() -> tuple:
    """Premiere Pro の起動 + CEP (Premiere Link) パネル疎通を確認。

    Returns: (ok: bool, message: str)
    pipeline の steps に "premiere" / "export" が含まれる場合のみ呼ぶ。
    """
    try:
        if str(BASE) not in sys.path:
            sys.path.insert(0, str(BASE))
        from app_premiere import _is_premiere_running, _panel_alive
    except Exception as e:
        return False, f"app_premiere の import に失敗: {e}"
    if not _is_premiere_running():
        return False, "Premiere Pro が起動していません。"
    if not _panel_alive():
        return False, "Premiere Link CEP パネルが応答していません（30 秒以内 ping なし）。"
    return True, "Premiere Pro + CEP panel 疎通 OK"


RUNBOOK_HINT = "📖 docs/runbook.md（共有ドライブ）に対応手順あり"


def _notify_discord(message: str) -> None:
    """app_notify.sh 経由で Discord に投げる。失敗しても何もしない（通知失敗で本体を止めない）。

    全メッセージの末尾に runbook の参照を自動付与する。"""
    notify = BASE / "app_notify.sh"
    if not notify.exists():
        return
    full_message = f"{message}\n\n{RUNBOOK_HINT}"
    try:
        subprocess.run(
            ["bash", str(notify), full_message],
            timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass

def _notify_line(message: str) -> None:
    """Backward-compatible alias for older code paths."""
    _notify_discord(message)


def _api_post(path, body=None, label="", timeout=10):
    """Web API を叩く（localhost:8888 が起動している前提）。
    timeout: 非同期キック系は既定10秒で十分。LLM/翻訳など POST レスポンスで結果を返す
             同期エンドポイントは呼び出し側で長め(例 300)を渡すこと。"""
    import urllib.request
    url = f"{API_BASE}{path}"
    data = json.dumps(body or {}).encode() if body else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
            print(f"  ✓ API {path}: {result.get('status', 'ok')}")
            return result
    except Exception as e:
        print(f"  ❌ API {path} 失敗: {e}")
        return None


def _api_poll(status_path, label, timeout=7200, interval=3):
    """API のステータスをポーリングして完了を待つ。
    完了判定: 一度 running=True を観測した後に running=False かつ末尾ログに [完了] 終了コード: 0。
    ⚠ running=True を一度も観測しない間は、前ジョブの残存完了マーカーを新ジョブ完了と
       誤認しないよう採用しない（premiere→export で /api/premiere/status を共有するため）。
    """
    import urllib.request
    start = time.time()
    last_running_seen = False
    start_grace = 60  # running=True を観測するまでの猶予(秒)。超えたら起動失敗とみなす
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(f"{API_BASE}{status_path}", timeout=5) as resp:
                d = json.loads(resp.read())
                if d.get("running"):
                    last_running_seen = True
                else:
                    logs = d.get("logs", []) or []
                    tail = "\n".join(logs[-5:])
                    if last_running_seen:
                        # 自ジョブが走った後の完了マーカーのみ信用する
                        if "[完了] 終了コード: 0" in tail:
                            print(f"\n✅ {label} 完了")
                            return True
                        if "[完了] 終了コード:" in tail:
                            print(f"\n❌ {label} 失敗: {tail.splitlines()[-1] if tail else '?'}")
                            return False
                        # running=False だがマーカー未達 → ログ遅延の猶予
                        time.sleep(interval)
                        continue
                    else:
                        # まだ起動前。前ジョブの残存マーカーは無視。猶予超過なら起動失敗で打ち切る
                        if time.time() - start > start_grace:
                            print(f"\n❌ {label} 起動確認失敗: {start_grace}s 以内に running=True 未観測")
                            return False
        except Exception:
            pass
        time.sleep(interval)
    print(f"\n⏰ {label} タイムアウト ({timeout}s)")
    return False


# ─── 各工程 ───

def _load_benchmark_axes() -> dict:
    """P3-2: concept / title / thumbnail の 3 軸 aggregate を集めて返す。

    これらは P2 で各 axis が独立に書き出した json を読むだけ（Claude CLI は呼ばない）。
    """
    axes = {"concept": None, "title": None, "thumbnail": None}
    for axis in axes:
        f = CONFIG_DIR / "benchmark" / f"{axis}.json"
        used_scoped_loader = False
        try:
            from app_channel_cache import load_scoped_cache
            used_scoped_loader = True
            d = load_scoped_cache(f"{axis}.json", f, None)
        except Exception:
            d = None
        if d is None and not used_scoped_loader and f.exists():
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                d = None
        if not isinstance(d, dict):
            continue
        if axis == "thumbnail":
            axes[axis] = (
                d.get("aggregate")
                or ((d.get("analysis") or {}).get("aggregate") if isinstance(d.get("analysis"), dict) else {})
                or {}
            )
        else:
            axes[axis] = d.get("aggregate") or {}
    return axes


def _score_plan_via_claude(cli: str, plan: dict, persona: str) -> tuple:
    """Claude CLI に plan の品質を 0〜10 で採点させる。

    Returns: (score: float, feedback: str)
    score 失敗時は (5.0, '') で「中立判定」を返し、再生成しないようにする。
    """
    prompt = f"""You are evaluating a video production plan for a YouTube BGM channel.
Rate the plan on 0-10 considering: (1) clarity of suno_prompt, (2) alignment with persona,
(3) coverage of benchmark insights (themes, audience, visual direction), (4) actionability.

Persona: {(persona or '(unspecified)')[:300]}

Plan (JSON):
```json
{json.dumps(plan, ensure_ascii=False, indent=2)[:2000]}
```

Output a single JSON object (no markdown fences):
{{"score": 7.5, "feedback": "1〜2 文の改善ポイント"}}"""
    from app_llm_runner import run_llm
    try:
        text = run_llm(prompt, cli_cmd=cli, timeout=60, label="plan-score")
    except Exception as e:
        print(f"    ⚠ scoring 失敗（中立判定）: {e}")
        return 5.0, ""
    m = re.search(r"\{[^{}]*\"score\"[^{}]*\}", text, re.DOTALL)
    if not m:
        return 5.0, ""
    try:
        d = json.loads(m.group(0))
        score = float(d.get("score", 5.0))
        feedback = (d.get("feedback") or "")[:300]
        return max(0.0, min(10.0, score)), feedback
    except Exception:
        return 5.0, ""


def step_plan(vol: int, folder: Path, via_api: bool, **kw):
    """ベンチマーク分析キャッシュ + ペルソナ → 次動画プラン (plan.json) を生成。
    後続の step_suno は plan.json があればそれを優先利用する。

    P3-2: concept/title/thumbnail の 3 軸 aggregate を取り込んだ enriched plan に拡張。
    Claude CLI で品質スコアを付け、閾値（既定 7.0）未満なら最大 3 回まで再生成。
    `APP_PLAN_AUTO_ADOPT_THRESHOLD` で閾値、`APP_PLAN_MAX_ATTEMPTS` で試行回数を上書き可。
    既存 plan.json があれば再利用（再生成したい場合は事前削除）。
    """
    plan_path = folder / "plan.json"
    cli = _load_suno_config().get("claude_cli", "claude")
    cfg = _load_dashboard_config()
    persona = cfg.get("persona") or ""

    print(f"\n{'='*60}")
    print(f"  {STEP_LABELS['plan']}")
    print(f"{'='*60}\n")

    # 既存 plan.json がスコア付きなら再利用
    if plan_path.exists():
        try:
            existing = json.loads(plan_path.read_text(encoding="utf-8"))
            if existing.get("quality_score") is not None and existing.get("suno_prompt"):
                print(f"  ⏭ 既存 plan.json を再利用 (score={existing.get('quality_score')})")
                return True
        except Exception:
            pass

    try:
        import app_competitor as _ac
        cache = _ac.load_cache() or {}
    except Exception:
        cache = {}
    if not cache:
        print("  ⚠️ 競合分析キャッシュが見つかりません")
        print("     先に Web UI で「📡 競合データ取得 + 分析」を実行してください")
        print("     スキップして既定プロンプトで進みます")
        return True

    threshold = float(os.environ.get("APP_PLAN_AUTO_ADOPT_THRESHOLD", "7.0"))
    max_attempts = int(os.environ.get("APP_PLAN_MAX_ATTEMPTS", "3"))
    benchmark_axes = _load_benchmark_axes()

    try:
        analysis = cache.get("analysis", {})
        if not analysis.get("music_direction"):
            print("  ⚠️ analysis.music_direction が空。スキップ")
            return True
        sys.path.insert(0, str(BASE))
        from app_benchmark_analyze import propose_suno_prompt

        best_plan = None
        best_score = -1.0
        best_feedback = ""
        for attempt in range(1, max_attempts + 1):
            proposal = propose_suno_prompt(analysis, cli_cmd=cli)
            plan = {
                "vol": vol,
                "video_name": folder.name,
                "suno_prompt": proposal.get("prompt", ""),
                "rationale": proposal.get("rationale", ""),
                "source": "benchmark_analysis",
                "analyzed_at": cache.get("analyzed_at", ""),
                # P3-2: 3 軸 aggregate を含めて enriched 化
                "benchmark_axes_aggregate": {
                    k: v for k, v in benchmark_axes.items() if v is not None
                },
                "attempt": attempt,
            }
            score, feedback = _score_plan_via_claude(cli, plan, persona)
            print(f"  [attempt {attempt}/{max_attempts}] score={score:.1f} threshold={threshold}")
            if feedback:
                print(f"    feedback: {feedback[:120]}")
            if score > best_score:
                best_plan = plan
                best_score = score
                best_feedback = feedback
            if score >= threshold:
                break  # 閾値達成 → 採択

        if best_plan is None:
            print("  ⚠️ どの試行でも plan を生成できず → スキップ")
            return True
        best_plan["quality_score"] = best_score
        best_plan["quality_feedback"] = best_feedback
        best_plan["adopted"] = best_score >= threshold
        plan_path.write_text(
            json.dumps(best_plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  ✓ plan.json 保存: {plan_path}")
        print(f"  最終 score: {best_score:.1f} / threshold {threshold} → "
              f"{'AUTO-ADOPTED' if best_plan['adopted'] else '採択（閾値未満・要見直し）'}")
        if best_plan.get("suno_prompt"):
            print(f"  prompt: {best_plan['suno_prompt'][:100]}…")
        return True
    except Exception as e:
        print(f"  ❌ プラン生成失敗: {e}")
        print("     既定プロンプトで進みます")
        return True


def step_suno(vol: int, folder: Path, via_api: bool, **kw):
    # SUNO 設定は per-channel `.app_channel_config.json.suno` を最優先、
    # 無ければグローバル `~/.config/{app_id}/suno_config.json` にフォールバック。
    # 3 チャンネル並列運用でプロンプト/loop_count/provider 等が混ざらないようにするため。
    channel_cfg = _load_channel_suno_config()
    global_cfg = _load_suno_config()

    def _cfg(key, default=None):
        """per-channel > global > default の順で値を返す（"" / None は未設定扱い）"""
        v = channel_cfg.get(key)
        if v not in (None, ""):
            return v
        v = global_cfg.get(key)
        if v not in (None, ""):
            return v
        return default

    # plan.json があれば最優先で suno_prompt を採用
    plan_prompt = ""
    plan_path = folder / "plan.json"
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan_prompt = plan.get("suno_prompt", "")
            if plan_prompt:
                print(f"  📋 plan.json のプロンプトを使用")
        except Exception:
            pass
    # env 上書き（ダッシュボードのフォーム値）
    # プロンプト確認ロジック: 暗黙の文字列フォールバック（"Create a sophisticated lounge BGM track."）
    # は廃止。明示プロンプトが無ければここで停止し、ユーザーに必ず指定させる。
    prompt_sources = [
        ("APP_SUNO_PROMPT env",       (os.environ.get("APP_SUNO_PROMPT") or "").strip()),
        ("ORZZ_SUNO_PROMPT env",      (os.environ.get("ORZZ_SUNO_PROMPT") or "").strip()),
        ("plan.json",                 (plan_prompt or "").strip()),
        ("channel config (.app_channel_config.json)",
                                      (channel_cfg.get("prompt") or "").strip()),
        ("global suno_config.json",   (global_cfg.get("prompt") or "").strip()),
    ]
    prompt = ""
    prompt_source = ""
    for src, val in prompt_sources:
        if val:
            prompt = val
            prompt_source = src
            break
    if not prompt:
        print(
            "❌ SUNO プロンプトが見つかりません。以下のいずれかで明示してください:\n"
            "  - 環境変数 APP_SUNO_PROMPT を設定\n"
            "  - channel config (.app_channel_config.json) の suno.prompt にプロンプト本文を保存\n"
            "  - pipeline 呼び出し側で plan_prompt を渡す"
        )
        return False
    print(f"  ✔ prompt 確認 OK [{prompt_source}] ({len(prompt)} chars): "
          f"{prompt[:80]}{'…' if len(prompt) > 80 else ''}")
    count = int((os.environ.get("APP_SUNO_COUNT") or os.environ.get("ORZZ_SUNO_COUNT")) or kw.get("suno_count") or _cfg("loop_count", 5))
    interval = int((os.environ.get("APP_SUNO_INTERVAL") or os.environ.get("ORZZ_SUNO_INTERVAL")) or kw.get("suno_interval") or _cfg("loop_interval_sec", 60))
    provider = (
        os.environ.get("APP_SUNO_PROVIDER")
        or os.environ.get("ORZZ_SUNO_PROVIDER")
        or _cfg("provider")
        or "claude"
    )
    mode = (os.environ.get("APP_SUNO_MODE") or os.environ.get("ORZZ_SUNO_MODE")) or _cfg("generation_mode", "styles_title_only")
    batch_env = (os.environ.get("APP_SUNO_BATCH") or os.environ.get("ORZZ_SUNO_BATCH"))
    if batch_env is not None:
        batch = (batch_env == "1")
    else:
        # per-channel `loop_batch` を尊重（None なら provider ベースのデフォルト）
        ch_batch = channel_cfg.get("loop_batch")
        gl_batch = global_cfg.get("loop_batch")
        if ch_batch is not None:
            batch = bool(ch_batch)
        elif gl_batch is not None:
            batch = bool(gl_batch)
        else:
            batch = (provider in ("claude", "codex"))
    dl_wait = int((os.environ.get("APP_DL_WAIT_SEC") or os.environ.get("ORZZ_DL_WAIT_SEC")) or 30)

    # 多様性制御（env 上書き > per-channel suno block > suno 側の既定）。未設定は None のまま渡さない。
    def _norm_num(v, is_int):
        if v in (None, ""):
            return None
        try:
            return int(float(v)) if is_int else float(v)
        except (TypeError, ValueError):
            return None
    div_threshold = _norm_num(os.environ.get("APP_SUNO_DIVERSITY_THRESHOLD") or _cfg("diversity_threshold"), False)
    div_retry = _norm_num(os.environ.get("APP_SUNO_DIVERSITY_RETRY") or _cfg("diversity_retry"), True)
    hist_limit = _norm_num(os.environ.get("APP_SUNO_HISTORY_LIMIT") or _cfg("history_limit"), True)

    ch_cfg = _load_dashboard_config()
    ch_name = re.sub(r"[^A-Za-z0-9_-]+", "_", ch_cfg.get("channel_name", "orzz")).strip("_") or "orzz"
    workspace = f"{ch_name}_vol{vol}"

    print(f"  prompt: {prompt[:60]}{'…' if len(prompt) > 60 else ''}")
    print(f"  count={count}, interval={interval}, provider={provider}, mode={mode}, batch={batch}")
    print(f"  post-DL: wait={dl_wait}s")

    if via_api:
        body = {
            "prompt": prompt, "count": count, "interval": interval,
            "provider": provider, "generation_mode": mode, "batch": batch,
            "workspace": workspace, "video_name": folder.name,
        }
        if div_threshold is not None:
            body["diversity_threshold"] = div_threshold
        if div_retry is not None:
            body["diversity_retry"] = div_retry
        if hist_limit is not None:
            body["history_limit"] = hist_limit
        r = _api_post("/api/suno/start", body, "SUNO 生成開始")
        if not r:
            return False
        ok = _api_poll("/api/suno/status", "SUNO 生成")
        if not ok:
            return False
        # 生成完了 → 待機 → DL → 短尺削除
        if dl_wait > 0:
            print(f"\n  ⏳ DL 開始まで {dl_wait} 秒待機")
            time.sleep(dl_wait)
        print(f"\n  ⬇ Workspace DL 開始")
        dl = _api_post("/api/suno/download", {"video_name": folder.name}, "Workspace DL")
        if not dl:
            print("  ❌ DL リクエスト失敗（API レスポンスなし）")
            return False
        dl_ok = _api_poll("/api/suno/status", "Workspace DL", timeout=3600)
        if not dl_ok:
            print("  ❌ DL 完了確認失敗")
            return False
        # 次の step（process-tracks）が active_tasks の競合を起こさないよう短いクールダウン
        time.sleep(2)
        return True
    else:
        cmd = [
            sys.executable, str(BASE / "suno_auto_create.py"),
            "--prompt", prompt, "--count", str(count), "--interval", str(interval),
            "--provider", provider,
            "--workspace", workspace,
        ]
        if batch:
            cmd += ["--batch"]
        if div_threshold is not None:
            cmd += ["--diversity-threshold", str(div_threshold)]
        if div_retry is not None:
            cmd += ["--diversity-retry", str(div_retry)]
        if hist_limit is not None:
            cmd += ["--history-limit", str(hist_limit)]
        # 絶対 timeout: count × (interval + 60s) + 起動/最終 close 余裕 600s。
        # 子プロセスが固まっても親側で打ち切れるようにする（最終曲ハング対策）。
        suno_timeout = count * (interval + 60) + 600
        # Cloudflare Bot 判定対策: 既定で APP_KEEP_BROWSER=1 を立てる
        suno_env_overrides = {}
        if os.environ.get("APP_KEEP_BROWSER", "").strip() not in ("0", "false", "no"):
            suno_env_overrides["APP_KEEP_BROWSER"] = "1"
        suno_ok = _run(cmd, STEP_LABELS["suno"], timeout=suno_timeout,
                       env_overrides=suno_env_overrides or None)

        # ─── 15/15 完了後の自動連携（DL + リネーム + フェード）─────────────────
        # SUNO は最終曲を投入後も裏で生成が走っているため、N 秒待ってから DL する。
        # APP_SUNO_AUTO_DOWNLOAD=0 で無効化可能（生成だけしたいケース用）。
        auto_dl = os.environ.get("APP_SUNO_AUTO_DOWNLOAD", "1").strip() not in ("0", "false", "no")
        if not auto_dl:
            return suno_ok

        # SUNO が途中で失敗（Bot 判定タイムアウト / 認証期限 等）しても、
        # 既に SUNO workspace に貯まっている楽曲を回収するため DL に進む。
        # APP_SUNO_CONTINUE_ON_PARTIAL=0 で無効化可能（失敗時に即停止する従来動作）。
        continue_on_fail = os.environ.get("APP_SUNO_CONTINUE_ON_PARTIAL", "1").strip() not in ("0", "false", "no")
        if not suno_ok:
            if not continue_on_fail:
                return suno_ok
            print("  ⚠ SUNO 生成は中断しましたが、生成済み楽曲を回収するため DL に進みます")
            print("     （APP_SUNO_CONTINUE_ON_PARTIAL=0 で従来動作 = 失敗時即停止）")

        final_wait = int(os.environ.get("APP_SUNO_FINAL_WAIT_SEC") or "300")
        if final_wait > 0:
            print(f"\n  ⏳ 最終曲の生成完了を待機: {final_wait} 秒（APP_SUNO_FINAL_WAIT_SEC で変更可）")
            time.sleep(final_wait)

        print(f"\n  ⬇ Workspace DL 開始: {workspace} → {folder}")
        dl_cmd = [
            sys.executable, str(BASE / "suno_auto_create.py"),
            "--download-workspace", workspace,
            "--download-dir", str(folder),
        ]
        dl_ok = _run(dl_cmd, "SUNO ダウンロード", timeout=3600)
        if not dl_ok:
            print("  ⚠ ダウンロード失敗（生成は完了済み、後で `--download-workspace` を手動実行可）")
            return False

        # リネーム + フェード（既存の step_rename ロジックを subprocess で利用）
        print(f"\n  🎵 リネーム + フェード処理")
        process_cmd = [
            sys.executable, str(BASE / "app_process_tracks.py"),
            str(folder),
        ]
        process_ok = _run(process_cmd, "リネーム + フェード", timeout=1800)
        if not process_ok:
            print("  ⚠ 後処理失敗（DL までは完了、`app_process_tracks.py <folder>` で手動実行可）")
            # 後処理失敗は警告のみ。DL までは成功なのでステップ全体は成功扱い。
        return True


def _tag_args_from_channel(vol: int, folder: Path):
    """channel config の suno.tag_defaults から整備(タグ付与)用の引数を組む。

    tag_defaults が無いチャンネルは ([], {}) を返す＝整備しない従来挙動を維持。
    戻り値: (cli_extra_args, api_query) のタプル。
      cli_extra_args: app_process_tracks.py に渡す ['--apply-tags', '--artist', ...]
      api_query: /api/videos/{name}/process-tracks のクエリ dict
    """
    td = (_load_channel_suno_config().get("tag_defaults") or {})
    if not td:
        return [], {}
    artist = str(td.get("artist") or "SUKIMA")
    genre_default = str(td.get("genre_default") or "Bossa Nova")
    album = None
    tmpl = td.get("album_template")
    if tmpl:
        album = str(tmpl).replace("{N}", str(vol))
    cli_extra = ["--apply-tags", "--artist", artist, "--genre", genre_default]
    # genre_by_kind は dict なので CLI 経路のみ --genre-map(JSON) で渡す。
    # API 経路は app.py 側が channel config から同じ値を補完するため query には載せない。
    gbk = td.get("genre_by_kind")
    if isinstance(gbk, dict) and gbk:
        cli_extra += ["--genre-map", json.dumps(gbk, ensure_ascii=False)]
    query = {"apply_tags": "true", "artist": artist, "genre": genre_default}
    if album:
        cli_extra += ["--album", album]
        query["album"] = album
    # keep_names: 整備時にタイトル再生成（サムネ/ペルソナ由来）をスキップし draft 正式名を維持。
    #   - channel config の `rename_keep_names` で明示制御。
    #   - 未設定なら genre_by_kind ありのチャンネル（=draft 駆動でタイトル/genre を持つ）を既定 True に。
    #     draft 由来の正式名を尊重しないと曲別 genre とタイトルが乖離するため。
    #   今日の手動フロー（/tmp/sukima_tag.py + --keep-names）と同じ挙動を pipeline に載せる。
    keep_names_cfg = _load_dashboard_config().get("rename_keep_names")
    if keep_names_cfg is None:
        keep_names = isinstance(gbk, dict) and bool(gbk)
    else:
        keep_names = bool(keep_names_cfg)
    if keep_names:
        cli_extra.append("--keep-names")
        query["keep_names"] = "true"
    return cli_extra, query


def step_rename(vol: int, folder: Path, via_api: bool, **kw):
    tag_cli, tag_query = _tag_args_from_channel(vol, folder)
    if via_api:
        if tag_query:
            from urllib.parse import quote
            qs = "&".join(f"{k}={quote(str(v))}" for k, v in tag_query.items())
            path = f"/api/videos/{folder.name}/process-tracks?{qs}"
        else:
            path = f"/api/videos/{folder.name}/process-tracks"
        r = _api_post(path, {}, "後処理開始")
        if not r:
            return False
        return _api_poll("/api/process/status", "後処理")
    else:
        cli = _load_suno_config().get("claude_cli", "claude")
        return _run([
            sys.executable, str(BASE / "app_process_tracks.py"),
            str(folder), "--cli", cli, *tag_cli,
        ], STEP_LABELS["rename"], timeout=1800)


def _use_render_queue() -> bool:
    """render queue を使うかどうか。

    APP_USE_RENDER_QUEUE=1（推奨・並列ジョブ向け）→ enqueue + wait
    APP_USE_RENDER_QUEUE=0 → 従来通り subprocess を直接実行
    未指定 → 0（既定オフ。P2 安定後に既定 ON へ切替予定）"""
    return os.environ.get("APP_USE_RENDER_QUEUE", "").strip() in ("1", "true", "yes")


def _enqueue_and_wait(stage: str, vol: int, folder: Path) -> bool:
    """render queue に enqueue → done/error/cancelled を blocking wait。
    成功なら True、失敗なら False。"""
    try:
        sys.path.insert(0, str(BASE))
        import app_render_queue as _rq
    except Exception as e:
        print(f"  ⚠ render queue モジュール読込失敗 → 直接実行へフォールバック: {e}")
        return None  # フォールバックを意味する sentinel
    cfg = _load_dashboard_config()
    ch_folder = cfg.get("channel_folder") or str(folder.parent)
    ch_name = cfg.get("channel_name") or ""
    try:
        jid = _rq.enqueue(
            channel_folder=ch_folder, channel_name=ch_name,
            vol=int(vol), video_name=folder.name, stage=stage,
        )
        print(f"  📥 enqueued render queue id={jid} (stage={stage}, vol={vol})")
        timeout = 3600 if stage == "premiere" else 7200
        job = _rq.wait_for(jid, timeout_sec=timeout)
        if job.get("status") == "done":
            print(f"  ✅ render queue id={jid} done ({job.get('duration_sec') or '-'}s)")
            return True
        msg = (job.get("error_message") or "")[:200]
        print(f"  ❌ render queue id={jid} {job.get('status')}: {msg}")
        return False
    except Exception as e:
        print(f"  ⚠ render queue 利用失敗 → 直接実行へフォールバック: {e}")
        return None


def step_bgimage(vol: int, folder: Path, via_api: bool, **kw):
    """背景画像生成（Premiere 自動配置前）。

    rival_channels の最初に登録されたチャンネルから
    ~/.config/{app_id}/benchmark/thumbs/{ch_id}/ にあるサムネを 3 枚ランダム選択し、
    チャンネル persona と組み合わせて codex_imagegen.py で背景画像を 1 枚生成。

    出力: <vol_folder>/vol{N}.png  （Premiere JSX のフォールバック規約と整合）
    既存 vol{N}.png/.jpg があればスキップ（regenerate 抑止）。

    env:
      APP_BGIMAGE_DISABLE=1     step 全体をスキップ
      APP_BGIMAGE_REFCOUNT=N    参照画像枚数（既定 3）
      APP_BGIMAGE_FORCE=1       既存があっても強制再生成
      APP_BGIMAGE_TIMEOUT_SEC=N  画像生成 1 件あたりのタイムアウト秒（既定 1800）
      APP_BGIMAGE_REFERENCE_IMAGE=/path/to/ref.jpg[:...]  固定参照画像（os.pathsep 区切り）
    """
    import random as _random
    print(f"  {STEP_LABELS.get('bgimage', '3/9 背景画像生成')}")

    if os.environ.get("APP_BGIMAGE_DISABLE", "").strip() in ("1", "true", "yes"):
        print("  ⊘ APP_BGIMAGE_DISABLE=1 によりスキップ")
        return True

    try:
        bg_timeout = max(60, int(os.environ.get("APP_BGIMAGE_TIMEOUT_SEC", "1800")))
    except ValueError:
        bg_timeout = 1800

    force = os.environ.get("APP_BGIMAGE_FORCE", "").strip() in ("1", "true", "yes")
    if not force:
        existing = (
            list(folder.glob(f"vol{vol}.png"))
            + list(folder.glob(f"vol{vol}_source.jpg"))
            + list(folder.glob(f"vol{vol}.jpg"))
        )
        if existing:
            print(f"  ⊘ 既存 {existing[0].name} あり、スキップ（再生成は APP_BGIMAGE_FORCE=1）")
            return True

    # ─── Web API 経由実行 ─────────────────────────
    # `--via-api` のときは Web Dashboard の /api/bgimage/run へ委譲。
    # サーバー側で _ensure_not_running("bgimage", ...) と task_logs["bgimage"] 蓄積を行うため、
    # 排他制御と UI 進捗表示は subprocess 直叩きより一段強い。
    if via_api:
        try:
            ref_count = int(os.environ.get("APP_BGIMAGE_REFCOUNT", "3"))
        except ValueError:
            ref_count = 3
        body = {
            "video_name": folder.name,
            "ref_count": ref_count,
            "force": force,
        }
        r = _api_post("/api/bgimage/run", body, "背景画像生成")
        if not r:
            return False
        return _api_poll("/api/bgimage/status",
                         STEP_LABELS.get('bgimage', '3/9 背景画像生成'),
                         timeout=bg_timeout + 180)

    cfg = _load_dashboard_config()
    persona = (cfg.get("persona") or "").strip()
    channel_name = (cfg.get("channel_name") or "").strip()
    if not persona:
        print("  ⚠ persona 未設定。チャンネル設定 → persona を入力してください（non-fatal でスキップ）")
        return True

    ref_count = 3
    try:
        ref_count = int(os.environ.get("APP_BGIMAGE_REFCOUNT", "3"))
    except ValueError:
        pass

    # 参照画像ソース優先順位:
    #   0. APP_BGIMAGE_REFERENCE_IMAGE（単発指定。最優先）
    #   1. per-channel config の reference_image_dir（UI で設定したフォルダパス。最優先）
    #   2. Picked（サムネ分析で人間が✓を入れたもの。canonical）
    #   3. rival_channels の thumbs フォルダ全体からランダム（最終フォールバック）
    ref_images: list[Path] = []

    # 0. 明示参照画像（今回の「このイメージに寄せたい」用）
    fixed_refs = (os.environ.get("APP_BGIMAGE_REFERENCE_IMAGE") or "").strip()
    if fixed_refs:
        for raw in fixed_refs.split(os.pathsep):
            p = Path(raw).expanduser()
            if p.exists() and p.is_file():
                ref_images.append(p)
            else:
                print(f"  ⚠ APP_BGIMAGE_REFERENCE_IMAGE が存在しません: {p}")
        ref_images = ref_images[:ref_count]
        if ref_images:
            print(f"  📎 固定参照画像 {len(ref_images)}/{ref_count} 枚")
            for r in ref_images:
                print(f"     - {r.name}")

    # 1. reference_image_dir（per-channel UI 設定）
    ref_dir_str = (cfg.get("reference_image_dir") or "").strip()
    if not ref_images and ref_dir_str:
        # ⚠ 移植性(2台Mac・ホーム名 abe_kota/asobimori): 相対パスは channel folder 基準、
        #   別マシンの絶対パスは「共有ドライブ」marker から現マシンのルートへ付け替え。
        channel_dir = folder.parent  # vol folder の親 = チャンネルフォルダ(現マシン解決済み)
        ref_dir = Path(ref_dir_str).expanduser()
        if not ref_dir.is_dir():
            _marker = "共有ドライブ"
            _s, _cur = str(ref_dir), str(channel_dir)
            if _marker in _s and _marker in _cur:
                _root = _cur[:_cur.index(_marker) + len(_marker)]
                ref_dir = Path(_root) / _s[_s.index(_marker) + len(_marker):].lstrip("/\\")
            elif not Path(ref_dir_str).is_absolute():
                ref_dir = channel_dir / ref_dir_str  # 相対 → チャンネルフォルダ配下
        if ref_dir.is_dir():
            dir_pool = (
                list(ref_dir.glob("*.jpg"))
                + list(ref_dir.glob("*.jpeg"))
                + list(ref_dir.glob("*.png"))
                + list(ref_dir.glob("*.webp"))
            )
            if dir_pool:
                _random.shuffle(dir_pool)
                ref_images = dir_pool[:ref_count]
                print(f"  🗂  reference_image_dir から {len(ref_images)}/{ref_count} 枚ランダム選択 ({ref_dir})")
                for r in ref_images:
                    print(f"     - {r.name}")
            else:
                print(f"  ⚠ reference_image_dir に画像なし: {ref_dir}（次のソースへフォールバック）")
        else:
            print(f"  ⚠ reference_image_dir が存在しません: {ref_dir}（次のソースへフォールバック）")

    # 2. Picked
    if not ref_images:
        try:
            from app_benchmark_thumbnail import get_picked_paths  # type: ignore
            picked = get_picked_paths(limit=ref_count) or []
            if picked:
                ref_images = [Path(p) for p in picked if Path(p).exists()][:ref_count]
                if ref_images:
                    print(f"  📌 Picked 参照画像 {len(ref_images)}/{ref_count} 枚（サムネ分析で選別済）")
                    for r in ref_images:
                        print(f"     - {r.name}")
        except Exception as e:
            print(f"  ⚠ Picked 取得失敗: {e}（rival_channels プールにフォールバック）")

    # 3. rival_channels の thumbs プール（最終フォールバック）
    if not ref_images:
        pool: list[Path] = []
        rival_channels = cfg.get("rival_channels") or []
        for url in rival_channels:
            m = re.search(r"channel/(UC[A-Za-z0-9_-]+)", str(url))
            if not m:
                continue
            ch_id = m.group(1)
            bench_dir = CONFIG_DIR / "benchmark" / "thumbs" / ch_id
            if bench_dir.exists():
                pool.extend(list(bench_dir.glob("*.jpg")) + list(bench_dir.glob("*.jpeg")) + list(bench_dir.glob("*.png")))
        if pool:
            _random.shuffle(pool)
            ref_images = pool[:ref_count]
            print(f"  📷 rival thumbs プール {len(pool)} 枚から {len(ref_images)}/{ref_count} 枚ランダム選択（最終フォールバック）")
            for r in ref_images:
                print(f"     - {r.parent.name}/{r.name}")
        else:
            print("  ⚠ reference_image_dir / Picked / rival thumbs どれも無し。参照無しで生成")

    # P: 背景プロンプトを動的構築（ベンチマーク concept/visual_direction 由来）。
    # APP_BGIMAGE_DYNAMIC_PROMPT=0 で旧固定テンプレに即ロールバック可能（移行安全弁）。
    use_dynamic = os.environ.get("APP_BGIMAGE_DYNAMIC_PROMPT", "1").strip().lower() not in ("0", "false", "no")
    if use_dynamic:
        try:
            base_prompt = _build_bgimage_prompt(folder, ref_images=ref_images)
            print("  📝 背景プロンプト: 参照3枚のVision共通要素＋ベンチマーク分析から動的構築")
        except Exception as e:
            print(f"  ⚠ 動的プロンプト構築失敗 → 固定テンプレにフォールバック: {e}")
            base_prompt = _legacy_bgimage_prompt(persona, channel_name)
    else:
        base_prompt = _legacy_bgimage_prompt(persona, channel_name)
    # ::vol{N}.png は最終行に独立して置く（codex_imagegen のファイル名コンベンション）
    prompt = f"{base_prompt}\n::vol{vol}.png"

    cmd = [
        sys.executable, str(BASE / "codex_imagegen.py"),
        "--output-dir", str(folder),
        "--prompt", prompt,
        "--quality", "high",
        "--output-format", "png",
        "--n", "1",
        "--timeout", str(bg_timeout),
    ]
    for ref in ref_images:
        cmd += ["--reference-image", str(ref)]

    if force:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        for old in (folder / f"vol{vol}.png", folder / f"vol{vol}_source.jpg"):
            if old.exists():
                backup = old.with_name(f"{old.stem}_rejected_{stamp}{old.suffix}")
                try:
                    old.rename(backup)
                    print(f"  ↩ 既存画像を退避: {old.name} → {backup.name}")
                except Exception as e:
                    print(f"  ⚠ 既存画像の退避失敗: {old.name}: {e}")

    ok = _run(
        cmd,
        STEP_LABELS.get('bgimage', '3/10 背景画像生成'),
        timeout=bg_timeout + 120,
    )
    if ok:
        # PNG 削除運用と両立するための source 用 JPG を並べて出す。
        # PSD 合成（step_psd_composite）の base 入力は AI 生成素材を必要とするが、
        # vol{N}.jpg は Photoshop 出力（PLAY LIST 焼き付き）なのでフォールバックには使えない。
        # ここで生成しておく vol{N}_source.jpg は PLAY LIST 等が一切入らない素の AI 画像のコピー。
        png_path = folder / f"vol{vol}.png"
        src_jpg = folder / f"vol{vol}_source.jpg"
        if png_path.exists() and not src_jpg.exists():
            try:
                subprocess.run(
                    ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "high",
                     str(png_path), "--out", str(src_jpg)],
                    check=True, capture_output=True, timeout=60,
                )
                print(f"  📷 source JPG 生成: {src_jpg.name}（PSD 合成フォールバック用）")
            except Exception as e:
                print(f"  ⚠ source JPG 生成失敗（PNG はそのまま、フォールバック無効）: {e}")
    return ok


def step_psd_composite(vol: int, folder: Path, via_api: bool, **kw):
    """PSD テンプレで vol{N}.jpg + サムネイル.jpg を 2 枚出し（背景画像 + サムネ）。

    bgimage step で AI 生成した <vol_folder>/vol{N}.png を per-channel テンプレ
    PSD の base スマートオブジェクト層に流し込み、LLM で生成した英語シーンコピーを
    都市名_テキスト層に set_text、PLAY LIST 層の表示/非表示を切り替えて 2 枚書き出す。

    出力:
      <vol_folder>/vol{N}.jpg     — 都市名OFF / PLAY LIST ON  （Premiere 背景画像用）
      <vol_folder>/サムネイル.jpg — 都市名ON  / PLAY LIST OFF （YouTube サムネ用）
      <vol_folder>/scene_en.txt   — 生成された英語シーンコピーのキャッシュ

    両出力が既に存在すればスキップ（再生成は APP_PSD_COMPOSITE_FORCE=1）。
    後続 step_thumbnail は サムネイル.jpg があると自動スキップする仕様なので、
    PSD 合成が成功すれば AI サムネ生成は走らない（フォールバックとして共存）。

    env:
      APP_PSD_COMPOSITE_DISABLE=1   step 全体をスキップ
      APP_PSD_COMPOSITE_FORCE=1     既存サムネ/背景があっても強制再合成（scene_*.txt は尊重）
      APP_SCENE_TEXT_REGEN=1        scene_en.txt / scene_ja.txt も作り直す（既定: 既存を再利用）
    """
    print(f"\n{'='*60}")
    print(f"  {STEP_LABELS.get('psd_composite', '4/10 PSD 合成')}")
    print(f"{'='*60}")

    if os.environ.get("APP_PSD_COMPOSITE_DISABLE", "").strip() in ("1", "true", "yes"):
        print("  ⊘ APP_PSD_COMPOSITE_DISABLE=1 によりスキップ")
        return True

    force = os.environ.get("APP_PSD_COMPOSITE_FORCE", "").strip() in ("1", "true", "yes")
    # FORCE は「画像合成のやり直し」専用フラグ。scene_en/scene_ja のテキストキャッシュ
    # 再生成は別フラグ（APP_SCENE_TEXT_REGEN）で制御する。FORCE だけで日本語/英語コピーが
    # 作り直されると、YouTube タイトル（scene_ja.txt 由来）とサムネ文字が不一致になる事故が
    # 起きるため（vol5 で実際に発生）。既定では既存の scene_*.txt を尊重する。
    scene_regen = os.environ.get("APP_SCENE_TEXT_REGEN", "").strip() in ("1", "true", "yes")
    out_bg = folder / f"vol{vol}.jpg"
    out_thumb = folder / "サムネイル.jpg"
    if not force and out_bg.exists() and out_thumb.exists():
        print(f"  ⊘ 既存 {out_bg.name} / {out_thumb.name} あり、スキップ（再生成は APP_PSD_COMPOSITE_FORCE=1）")
        return True

    # bgimage step が生成した AI 背景画像（必須入力）。
    # フォールバック順: vol{N}.png → vol{N}_source.jpg（PNG 削除運用と両立）。
    # vol{N}.jpg は Photoshop 出力（PLAY LIST 焼き付き）なので候補から除外する。
    base_image = folder / f"vol{vol}.png"
    if not base_image.exists():
        fallback = folder / f"vol{vol}_source.jpg"
        if fallback.exists():
            base_image = fallback
            print(f"  📷 vol{vol}.png 無し → フォールバック {fallback.name} を使用")
    if not base_image.exists():
        print(f"  ⚠ 背景画像 vol{vol}.png / vol{vol}_source.jpg どちらも無い（bgimage step を先に実行してください）→ スキップ")
        return True

    cfg = _load_dashboard_config()
    channel_folder_str = (cfg.get("channel_folder") or "").strip()
    if not channel_folder_str:
        print("  ⚠ channel_folder 未設定（アクティブチャンネルを設定してください）→ スキップ")
        return True
    template_psd_name = (cfg.get("template_psd") or "").strip()
    if not template_psd_name:
        print("  ⚠ template_psd 未設定（.app_channel_config.json）→ スキップ")
        return True
    template_psd = Path(channel_folder_str) / "プロジェクト" / template_psd_name
    if not template_psd.exists():
        print(f"  ⚠ テンプレ PSD が存在しません: {template_psd} → スキップ（手動で配置してから再実行）")
        return True

    # vol 固有 PSD パスを構築（フォルダ名 "<vol>_<prefix>_<YYMMDD>" の prefix を抽出）。
    # 例: "11_HN_260523" → "HN_vol11.psd"
    # vol_folder 作成プロセス（POST /api/videos/create 等）でテンプレ PSD が自動配置される
    # 前提なので、ここでは「無ければ即エラー」とする。コード側で勝手にコピーしてしまうと
    # vol 作成プロセスの不具合に気づけないため、明示的に失敗させて運用者にフォローを促す。
    fm = re.match(r"^\d+_([^_]+)", folder.name)
    folder_prefix = fm.group(1) if fm else "vol"
    # 正規名(ゼロ埋め無し) を優先。既存フォルダにありがちな ゼロ埋め名(sk_vol01.psd 等) や
    # 末尾サフィックス差にも耐性を持たせる（候補が全滅したときだけ失敗させ運用者にフォローを促す）。
    psd_path = folder / f"{folder_prefix}_vol{vol}.psd"
    if not psd_path.exists():
        cands = [psd_path]
        try:
            cands.append(folder / f"{folder_prefix}_vol{int(vol):02d}.psd")   # ゼロ埋め2桁
        except (ValueError, TypeError):
            pass
        try:
            for p in sorted(folder.glob(f"{folder_prefix}_vol*.psd")):
                mm = re.search(r"vol0*(\d+)", p.stem)
                if mm and int(mm.group(1)) == int(vol):
                    cands.append(p)
        except (ValueError, TypeError):
            pass
        found = next((c for c in cands if c.exists()), None)
        if found:
            psd_path = found
        else:
            try:
                padded = f"{folder_prefix}_vol{int(vol):02d}.psd"
            except (ValueError, TypeError):
                padded = f"{folder_prefix}_vol0{vol}.psd"
            print(f"  ❌ vol 固有 PSD が見つかりません: {folder_prefix}_vol{vol}.psd（ゼロ埋め名 {padded} も可）")
            print(f"     期待される配置: {folder / f'{folder_prefix}_vol{vol}.psd'}")
            print(f"     vol_folder 作成プロセスを確認してください（テンプレ {template_psd_name} の自動コピーが効いていない可能性）")
            print(f"     復旧後に再実行: python3 app_pipeline.py {vol} --only psd_composite")
            return False
    print(f"  📄 vol 固有 PSD を使用: {psd_path.name}")

    # NOTE: strip しない — PSD のレイヤー名には末尾スペースが含まれることがある
    # （例: Harbor Notes hn_base.psd の "PLAY LIST " は末尾スペース必須）
    base_layer = cfg.get("psd_base_layer") or "base"
    toggle_layer = cfg.get("psd_toggle_layer") or "PLAY LIST"
    # toggle 層を「背景=ON / サムネ=OFF で切替」ではなく「両出力で常時表示」にするか。
    # competitor 踏襲: headline（例 SUKIMA "Playlist"）は背景/サムネ両方で出すのが正しい。
    # 未設定/False なら従来の英語専用 toggle 挙動（orzz/Harbor Notes）と完全に同一。
    toggle_always_visible = bool(cfg.get("psd_toggle_always_visible"))
    psd_text_layer = cfg.get("psd_text_layer") or ""
    psd_text_font = cfg.get("psd_text_font") or ""  # PostScript 名（例: "HelveticaNeue-UltraLight"）。空ならコード側でフォント指定しない

    # 英語シーンコピー生成（LLM）— scene_en.txt にキャッシュ。
    # 文字のトーン/例/禁止語/構文はチャンネル別設定 scene_text_* から渡す（無ければ persona 中立）。
    # scene_text_enabled=false なら文字を入れない（単一トグル方式に流れる）。
    scene_enabled = cfg.get("scene_text_enabled")
    scene_enabled = True if scene_enabled is None else bool(scene_enabled)
    scene_file = folder / "scene_en.txt"
    scene_text = ""
    if not scene_enabled:
        print("  ⊘ scene_text_enabled=false → シーン文字を入れない（単一トグル方式で続行）")
    else:
        if scene_file.exists() and not scene_regen:
            try:
                scene_text = scene_file.read_text(encoding="utf-8").strip()
            except Exception:
                scene_text = ""
        if not scene_text:
            persona = (cfg.get("persona") or "").strip()
            cli = _load_suno_config().get("claude_cli", "claude")
            scene_text = _generate_scene_copy_en(
                cli=cli, persona=persona, folder_name=folder.name, vol=vol,
                tone=(cfg.get("scene_text_tone") or ""),
                examples=cfg.get("scene_text_examples") or [],
                forbidden=cfg.get("scene_text_forbidden") or [],
                structure=(cfg.get("scene_text_structure") or ""),
            )
            if scene_text:
                try:
                    scene_file.write_text(scene_text + "\n", encoding="utf-8")
                    print(f"  💬 scene_en: {scene_text!r}（{scene_file.name} に保存）")
                except Exception as e:
                    print(f"  ⚠ scene_en.txt 書き込み失敗: {e}（続行）")
            else:
                print("  ⚠ scene_en 生成失敗。空文字で続行（テキスト層は変更されません）")

    # 日本語前向きキャッチコピー生成（LLM）— scene_ja.txt にキャッシュ。
    # competitor 風サムネ（「Playlist.（英）大 + 日本語コピー1行 + 英語サブ小」）の
    # 日本語コピー部分を担当。英語専用挙動とは scene_text_ja_enabled で完全分離する。
    # scene_text_ja_enabled が未設定/false なら一切走らない（既存挙動と完全に同一）。
    scene_ja_enabled = bool(cfg.get("scene_text_ja_enabled"))
    scene_ja_text = ""
    scene_ja_layer = (cfg.get("scene_text_ja_layer") or "").strip()
    scene_ja_font = (cfg.get("scene_text_ja_font") or "").strip()
    if scene_ja_enabled:
        if not scene_ja_layer:
            print("  ⚠ scene_text_ja_enabled=true だが scene_text_ja_layer 未設定 → 日本語コピーをスキップ（英語のみで続行）")
        else:
            scene_ja_file = folder / "scene_ja.txt"
            if scene_ja_file.exists() and not scene_regen:
                try:
                    scene_ja_text = scene_ja_file.read_text(encoding="utf-8").strip()
                except Exception:
                    scene_ja_text = ""
            if not scene_ja_text:
                persona = (cfg.get("persona") or "").strip()
                cli = _load_suno_config().get("claude_cli", "claude")
                scene_ja_text = _generate_scene_copy_ja(
                    cli=cli, persona=persona, folder_name=folder.name, vol=vol,
                    tone=(cfg.get("scene_text_ja_tone") or ""),
                    examples=cfg.get("scene_text_ja_examples") or [],
                    forbidden=cfg.get("scene_text_ja_forbidden") or [],
                    structure=(cfg.get("scene_text_ja_structure") or ""),
                )
                if scene_ja_text:
                    try:
                        scene_ja_file.write_text(scene_ja_text + "\n", encoding="utf-8")
                        print(f"  💬 scene_ja: {scene_ja_text!r}（{scene_ja_file.name} に保存・layer={scene_ja_layer!r}）")
                    except Exception as e:
                        print(f"  ⚠ scene_ja.txt 書き込み失敗: {e}（続行）")
                else:
                    print("  ⚠ scene_ja 生成失敗。空文字で続行（日本語テキスト層は変更されません）")

    # 書き出し解像度 — PSD キャンバスが 1280×720 等でも YouTube/Premiere 用に 1920×1080 へ
    # アップサンプル（BICUBICSMOOTHER）。per-channel で psd_export_width/height を指定可能。
    try:
        target_width = int(cfg.get("psd_export_width") or 1920)
    except (TypeError, ValueError):
        target_width = 1920
    try:
        target_height = int(cfg.get("psd_export_height") or 1080)
    except (TypeError, ValueError):
        target_height = 1080

    try:
        sys.path.insert(0, str(BASE))
        from app_photoshop import render_dual_thumbnail
        extra = {}
        if psd_text_layer:
            extra["scene_text_layer"] = psd_text_layer
        if psd_text_font:
            extra["scene_text_font"] = psd_text_font
        # 日本語コピー（competitor 風）— enabled かつ layer/text が揃ったときだけ渡す。
        # 渡さなければ render_dual_thumbnail の挙動は従来の英語専用と完全に同一。
        if scene_ja_enabled and scene_ja_layer and scene_ja_text:
            extra["scene_text_ja"] = scene_ja_text
            extra["scene_text_ja_layer"] = scene_ja_layer
            if scene_ja_font:
                extra["scene_text_ja_font"] = scene_ja_font
        # 背景(vol{N}.jpg)を base レイヤーのみで出力 / テキストを元レイヤー位置で維持
        # （per-channel 設定。キー無し＝従来挙動を完全維持＝他チャンネル不変）
        if cfg.get("psd_bg_base_only"):
            extra["bg_base_only"] = True
        if cfg.get("psd_center_text") is False:
            extra["center_text"] = False
        result = render_dual_thumbnail(
            psd_path=str(psd_path),
            base_image=str(base_image),
            scene_text=scene_text or "",
            out_dir=str(folder),
            vol_name=f"vol{vol}",
            base_layer=base_layer,
            playlist_layer=toggle_layer,
            toggle_always_visible=toggle_always_visible,
            target_width=target_width,
            target_height=target_height,
            save_psd=True,
            **extra,
        )
        print(f"  ✅ bg: {Path(result.get('bg', '?')).name} / thumbnail: {Path(result.get('thumbnail', '?')).name}")
        return True
    except FileNotFoundError as e:
        print(f"  ⚠ ファイル不在: {e}")
        return False
    except Exception as e:
        print(f"  ❌ PSD 合成失敗: {e}")
        return False


def _generate_scene_copy_en(*, cli: str, persona: str, folder_name: str, vol: int,
                            tone: str = "", examples=None, forbidden=None,
                            structure: str = "") -> str:
    """LLM に persona / フォルダ名 / vol + チャンネル別文字設定を渡して英語シーンコピーを 1 件生成。

    文字のトーン・例・禁止語・構文は **チャンネル設定（scene_text_*）から渡す**設計。
    旧仕様でハードコードしていた特定チャンネル（Harbor Notes）由来のルールは撤去済み。
    引数が空のときは persona 準拠の中立指示で生成（特定チャンネル色を出さない）。

    Returns: 全大文字 2〜3 語の英語フレーズ（例: 'BLUE HORIZON'）。失敗時は空文字。
    """
    examples = examples or []
    forbidden = forbidden or []
    tone_line = (tone or "").strip() or "a mood that fits the channel persona above"
    structure_line = (structure or "").strip() or "verb+noun or adjective+noun"
    examples_block = ("  - Style reference (match the register, do NOT copy verbatim): " + " / ".join(examples) + "\n") if examples else ""
    forbidden_block = ("  - Forbidden exact matches (never output these): " + ", ".join(forbidden) + "\n") if forbidden else ""
    prompt = (
        "You generate a short English scene caption for a long-form BGM YouTube thumbnail.\n"
        f"Channel persona: {persona or '(unspecified)'}\n"
        f"Folder/video name: {folder_name} (vol.{vol})\n"
        "Rules:\n"
        "  - Output ONE phrase, ALL UPPERCASE, English only.\n"
        "  - 2 to 3 words (4 words OK only if natural).\n"
        f"  - Structure: {structure_line}.\n"
        f"  - Tone: {tone_line}.\n"
        f"{examples_block}{forbidden_block}"
        "  - No quotes, no surrounding punctuation, no emojis, no labels, no explanation.\n"
        "Output only the phrase, nothing else."
    )
    try:
        from app_llm_runner import run_llm
        out = run_llm(prompt, cli_cmd=cli, timeout=120, label="thumb-phrase")
        for line in out.splitlines():
            line = line.strip().strip('"').strip("'").rstrip(".,!?")
            if line:
                return line
        return ""
    except Exception:
        return ""


def _generate_scene_copy_ja(*, cli: str, persona: str, folder_name: str, vol: int,
                            tone: str = "", examples=None, forbidden=None,
                            structure: str = "") -> str:
    """LLM に persona / フォルダ名 / vol + チャンネル別文字設定を渡して日本語コピーを 1 件生成。

    competitor 風サムネ（「Playlist.（英）大 + 日本語コピー1行 + 英語サブ小」）の
    日本語キャッチコピー部分（例「軽やかに、一日が動き出す。」）を生成する。
    英語シーンコピー（_generate_scene_copy_en）とは完全に独立。
    トーン・例・禁止語・構文は **チャンネル設定（scene_text_ja_*）から渡す**設計。

    生成方針:
      - 前向き・cozy・カフェの朝（chanel persona 準拠）。読点込みで全角 2 行以内。
      - 句点「。」で終える 1 文（competitor 例「静かに没頭する時間に。」に倣う）。

    Returns: 日本語 1 文（句点込み）。失敗時は空文字。
    """
    examples = examples or []
    forbidden = forbidden or []
    tone_line = (tone or "").strip() or "チャンネルの世界観に合う、前向きで心地よいトーン"
    structure_line = (structure or "").strip() or "句点「。」で終える、自然な日本語 1 文（読点はあってよい）"
    examples_block = ("  - 文体の参考（レジスターを合わせる。そのままコピーしない）: " + " / ".join(examples) + "\n") if examples else ""
    forbidden_block = ("  - 使用禁止の語（出力に含めない）: " + "、".join(forbidden) + "\n") if forbidden else ""
    prompt = (
        "あなたは長尺 BGM YouTube サムネイル用の、日本語の短いキャッチコピーを 1 件だけ作ります。\n"
        f"チャンネルのペルソナ: {persona or '（未設定）'}\n"
        f"動画フォルダ/名称: {folder_name}（vol.{vol}）\n"
        "ルール:\n"
        "  - 出力は日本語のコピー 1 件のみ。説明・前置き・番号・引用符・絵文字は一切付けない。\n"
        f"  - 構文: {structure_line}。\n"
        f"  - トーン: {tone_line}。\n"
        "  - 全角で 2 行以内（おおむね 9〜16 文字程度の 1 文。長くしすぎない）。\n"
        "  - 前向き・cozy・カフェの朝のような心地よさ。ノスタルジー/悲しさ/夜/孤独は避ける。\n"
        f"{examples_block}{forbidden_block}"
        "  - 句点「。」で終える 1 文にする（例の文体に倣う）。\n"
        "コピー本文だけを出力してください。"
    )
    try:
        from app_llm_runner import run_llm
        # 日本語生成は Claude が timeout しやすい（タスク注記）。run_llm の Claude→Codex
        # フォールバックに加え、余裕を持った timeout を取る。
        out = run_llm(prompt, cli_cmd=cli, timeout=180, label="thumb-phrase-ja")
        for line in out.splitlines():
            line = line.strip().strip('"').strip("'").strip("「」『』")
            if line:
                return line
        return ""
    except Exception:
        return ""


def step_premiere(vol: int, folder: Path, via_api: bool, **kw):
    # duration 優先順位: kw["duration"] > APP_DURATION_SEC env > per-channel default_duration_sec > 10800
    duration = kw.get("duration")
    if duration is None:
        env_d = os.environ.get("APP_DURATION_SEC") or os.environ.get("ORZZ_DURATION_SEC")
        if env_d and env_d.strip().isdigit():
            duration = int(env_d.strip())
    if duration is None:
        ch_cfg = _load_dashboard_config()
        ch_default = ch_cfg.get("default_duration_sec")
        if isinstance(ch_default, (int, float)) and ch_default > 0:
            duration = int(ch_default)
    if duration is None:
        duration = 10800
    if _use_render_queue():
        result = _enqueue_and_wait("premiere", vol, folder)
        if result is not None:
            return result
        # フォールバック: 直接実行
    if via_api:
        r = _api_post("/api/premiere/run", {
            "video_name": folder.name,
            "duration": duration,
        }, "Premiere 自動配置")
        if not r:
            return False
        return _api_poll("/api/premiere/status", "Premiere 自動配置", timeout=3600)
    else:
        prproj = next(iter(folder.glob("*vol*.prproj")), None)
        cmd = [sys.executable, str(BASE / "app_premiere.py"),
               "--duration", str(duration)]
        if prproj:
            cmd += ["--project", str(prproj)]
        return _run(cmd, STEP_LABELS["premiere"], timeout=3600)


def _resolve_external_output_path(vol: int, folder: Path):
    """Web UI で設定された per-channel `export_path` を CLI 経路でも解決して
    `<export_path>/<video_name>/<prefix>_vol<num>.mp4` を返す。

    app.py の `_resolve_external_export_dir()` / `_resolve_output_path()` と
    挙動を揃える（API 経路と CLI 経路で書き出し先が一致するように）。

    解決手順:
      1. dashboard 設定の export_path を取得（空なら None）
      2. `{channel}` を channel_name で展開
      3. `/Volumes/<NAME>/...` の場合はマウント確認（未マウントなら警告 + None）
      4. `<export_path>/<folder.name>/` を mkdir
      5. file_prefix（dashboard 優先、無ければフォルダ命名規約から推定、最後は "vol"）で
         ファイル名を組み立てて返す
    """
    cfg = _load_dashboard_config()
    raw = (cfg.get("export_path") or "").strip()
    if not raw:
        return None

    channel_name = (cfg.get("channel_name") or "").strip()
    raw = raw.replace("{channel}", channel_name)
    ext_dir = Path(raw).expanduser()

    if str(ext_dir).startswith("/Volumes/"):
        try:
            parts = ext_dir.parts
            if len(parts) >= 3:
                vol_root = Path("/Volumes") / parts[2]
                if not vol_root.exists():
                    print(f"  ⚠ 外部ボリュームが未マウント: {vol_root} → vol_folder にフォールバック")
                    return None
        except Exception as e:
            print(f"  ⚠ export_path マウント確認エラー: {e} → vol_folder にフォールバック")
            return None

    m = re.match(r"^(\d+)_", folder.name)
    num = m.group(1) if m else str(vol)

    file_prefix = (cfg.get("file_prefix") or "").strip()
    if not file_prefix:
        fm = re.match(r"^\d+_([^_]+)", folder.name)
        file_prefix = fm.group(1) if fm else "vol"

    # 配置: <ext_dir>/<prefix>_vol{N}.mp4（flat 配置）
    # 旧: <ext_dir>/<vol_folder.name>/<prefix>_vol{N}.mp4 という per_video サブフォルダを
    # 作っていたが、SSD 側で vol ごとに大量のフォルダが増えて運用が雑然とするため、
    # flat 配置（既存 HN_vol01_1.mp4 / HN_vol06.mp4 等と同パターン）に揃える。
    return ext_dir / f"{file_prefix}_vol{num}.mp4"


def _backup_existing_meta(folder: Path) -> None:
    """既存の youtube_*.txt を *_backup.txt に退避（言語切替時の誤上書き防止）。"""
    import shutil as _sh
    for base in ("youtube_title", "youtube_description", "youtube_tags"):
        src = folder / f"{base}.txt"
        if src.exists():
            try:
                _sh.copy2(src, folder / f"{base}_backup.txt")
            except Exception:
                pass


def _channel_meta_lang_handle(cfg: dict) -> tuple:
    """per-channel 設定から (source_lang, handle) を解決。
    source_lang = youtube_upload_defaults.default_language（既定 "en"）。
    handle = channels.json レジストリの handle（説明文 CTA 用）。"""
    yd = cfg.get("youtube_upload_defaults") or {}
    source_lang = (yd.get("default_language") or "en").strip() or "en"
    handle = ""
    try:
        handle = _resolve_channel(channel_folder=cfg.get("channel_folder", "")).get("handle", "") or ""
    except Exception:
        handle = ""
    return source_lang, handle


def step_export(vol: int, folder: Path, via_api: bool, **kw):
    if _use_render_queue():
        result = _enqueue_and_wait("export", vol, folder)
        if result is not None:
            return result
        # フォールバック: 直接実行
    if via_api:
        r = _api_post("/api/premiere/export", {}, "書き出し")
        if not r:
            return False
        return _api_poll("/api/premiere/status", "書き出し", timeout=7200)
    else:
        prproj = next(iter(folder.glob("*vol*.prproj")), None) or next(iter(folder.glob("*.prproj")), None)
        cmd = [sys.executable, str(BASE / "app_premiere.py"), "--export-only"]
        if prproj:
            cmd += ["--project", str(prproj)]
        # Web UI で設定された export_path を CLI 経路でも反映（API 経路と挙動を揃える）
        ext_out = _resolve_external_output_path(vol, folder)
        if ext_out:
            cmd += ["--output-path", str(ext_out)]
            print(f"  📦 外部出力先: {ext_out}")
        return _run(cmd, STEP_LABELS["export"], timeout=7200)


def step_meta(vol: int, folder: Path, via_api: bool, **kw):
    """タイトル・説明・タグを Claude CLI で提案して保存"""
    cli = _load_suno_config().get("claude_cli", "claude")
    name = folder.name

    print(f"\n{'='*60}")
    print(f"  {STEP_LABELS['meta']}")
    print(f"{'='*60}")

    # ⚠ via_api の /suggest は同期LLM呼出で _api_post の10秒timeout に必ず引っかかり、
    #   タイトル/説明/タグが保存されないまま「メタ空のまま upload」する事故になる。
    #   そのため via_api 指定でも常に CLI 経路(claude_proposer 直呼)に一本化する。
    #   (CLI 経路は run_llm 経由で Claude→Codex フォールバックも効く。CLAUDE.md 堅牢性ルール)
    if True:
        # 直接 claude_proposer を使用
        sys.path.insert(0, str(BASE))
        from claude_proposer import propose_titles, propose_description, propose_tags, gather_context
        cfg = _load_dashboard_config()
        persona = cfg.get("persona", "")
        channel_name = cfg.get("channel_name", "orzz.")
        source_lang, handle = _channel_meta_lang_handle(cfg)
        print(f"  🌐 source_lang={source_lang} / channel={channel_name} / handle={handle or '-'}")
        ctx = gather_context(folder)
        _backup_existing_meta(folder)
        try:
            titles = propose_titles(cli_cmd=cli, persona=persona, channel_name=channel_name,
                                    source_lang=source_lang, count=5, **ctx)
            if titles:
                (folder / "youtube_title.txt").write_text(titles[0], encoding="utf-8")
                print(f"  ✓ タイトル: {titles[0]}")
        except Exception as e:
            print(f"  ⚠️ タイトル: {e}")
        try:
            desc = propose_description(cli_cmd=cli, persona=persona, channel_name=channel_name,
                                       source_lang=source_lang, handle=handle, **ctx)
            if desc:
                (folder / "youtube_description.txt").write_text(desc, encoding="utf-8")
                print(f"  ✓ 説明文: {len(desc)} 文字")
        except Exception as e:
            print(f"  ⚠️ 説明文: {e}")
        try:
            tags = propose_tags(cli_cmd=cli, persona=persona, channel_name=channel_name,
                                source_lang=source_lang, **ctx)
            if tags:
                (folder / "youtube_tags.txt").write_text("\n".join(tags), encoding="utf-8")
                print(f"  ✓ タグ: {len(tags)} 件")
        except Exception as e:
            print(f"  ⚠️ タグ: {e}")
        print(f"\n✅ {STEP_LABELS['meta']} 完了")
        return True


def step_localization(vol: int, folder: Path, via_api: bool, **kw):
    """多言語メタデータ生成。youtube_title/description.txt を localization_languages へ翻訳し
    `youtube_localizations.json` を生成。メイン言語（default_language）は除外。

    - per-channel `youtube_upload_defaults.default_language` をソース言語に。
    - `localization_languages` 未設定チャンネルは no-op（後方互換）。
    - 失敗しても upload は止めない（localizations が無くても本体メタで upload 可能）。
    """
    print(f"\n{'='*60}")
    print(f"  {STEP_LABELS.get('localization', '多言語メタデータ')}")
    print(f"{'='*60}")
    cfg = _load_dashboard_config()
    yd = cfg.get("youtube_upload_defaults") or {}
    source_lang = (yd.get("default_language") or "en").strip() or "en"
    langs = yd.get("localization_languages") or []
    targets = [l for l in langs if l and l != source_lang]
    if not targets:
        print("  ⏭ localization_languages 未設定 → スキップ")
        return True

    title_path = folder / "youtube_title.txt"
    desc_path = folder / "youtube_description.txt"
    if not title_path.exists() or not desc_path.exists():
        print("  ⚠ youtube_title.txt / youtube_description.txt が無い（meta 未実行？）→ スキップ")
        return True  # upload を止めない

    # ⚠ via_api の generate-localizations も同期翻訳で10秒timeout に切れるため、常に CLI 経路に一本化
    title = title_path.read_text(encoding="utf-8").strip()
    description = desc_path.read_text(encoding="utf-8").strip()
    cli = _load_suno_config().get("claude_cli", "claude")
    sys.path.insert(0, str(BASE))
    from claude_proposer import translate_metadata
    print(f"  🌐 {source_lang} → {', '.join(targets)}")
    try:
        result = translate_metadata(
            title=title, description=description,
            source_lang=source_lang, target_langs=targets, cli_cmd=cli,
        )
    except Exception as e:
        print(f"  ❌ 翻訳失敗: {e}")
        return False
    if not result:
        print("  ⚠ 翻訳結果が空 → スキップ（upload は続行可能）")
        return True
    out_path = folder / "youtube_localizations.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✅ {len(result)} 言語生成: {', '.join(result.keys())} → {out_path.name}")
    return True


def step_qa(vol: int, folder: Path, via_api: bool, **kw):
    """書き出し後 QA（P3-3）。アスペクト比 / 解像度 / 尺 / コーデックを ffprobe で確認。

    軽量チェック（数秒で完了）:
      - 解像度: 1920x1080 ± 許容（最低 1280x720）
      - アスペクト比: 16:9（許容 ±0.01）
      - 尺: 60s〜14400s（4時間）の範囲
      - コーデック: video=h264 / audio=aac

    重量チェック（オプション、`APP_QA_LOUDNESS=1`）:
      - 統合ラウドネス: -16 LUFS ± 2 LU（BGM 標準）

    NG 時は False を返し、pipeline は失敗扱い → auto_resume が `--from premiere` で
    Premiere に差し戻す（タイムライン or 書き出し設定の問題のはず）。

    出力: `<vol_folder>/qa_report.json`（次回起動時の参照や運営者の確認用）
    """
    print(f"\n{'='*60}")
    print(f"  {STEP_LABELS.get('qa', 'QA チェック')}")
    print(f"{'='*60}")
    if os.environ.get("APP_QA_DISABLE", "").strip() in ("1", "true", "yes"):
        print("  ⏭ APP_QA_DISABLE=1 によりスキップ")
        return True

    # 対象 mp4 を解決
    mp4 = next(iter(folder.glob("*vol*.mp4")), None) or next(iter(folder.glob("*.mp4")), None)
    if not mp4 or not mp4.exists():
        print(f"  ⚠ mp4 が見つからない（書き出し未完？） — QA をスキップ")
        return True  # mp4 が無い時点で upload も走らないので QA は素通し

    import shutil as _sh
    if not _sh.which("ffprobe"):
        print("  ⚠ ffprobe が未インストール → QA をスキップ（brew install ffmpeg）")
        return True

    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format",
        "-show_streams", str(mp4),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        print("  ❌ ffprobe タイムアウト")
        return False
    if r.returncode != 0:
        print(f"  ❌ ffprobe 失敗: {r.stderr[:200]}")
        return False
    try:
        info = json.loads(r.stdout)
    except Exception as e:
        print(f"  ❌ ffprobe JSON parse 失敗: {e}")
        return False

    # 解析
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    issues = []
    report = {
        "checked_at": __import__("datetime").datetime.now().isoformat(),
        "mp4": str(mp4),
        "duration_sec": float(fmt.get("duration") or 0),
        "size_mb": round(int(fmt.get("size") or 0) / 1024 / 1024, 1),
        "video": None,
        "audio": None,
        "issues": [],
        "passed": True,
    }
    # 動画 stream チェック
    if not v:
        issues.append("video stream が無い")
    else:
        w = int(v.get("width") or 0)
        h = int(v.get("height") or 0)
        codec = v.get("codec_name") or ""
        report["video"] = {"width": w, "height": h, "codec": codec,
                           "fps": v.get("avg_frame_rate")}
        if codec.lower() != "h264":
            issues.append(f"video codec={codec}（h264 推奨）")
        if h > 0:
            ratio = w / h if h else 0
            if not (1.76 <= ratio <= 1.79):
                issues.append(f"アスペクト比 {ratio:.3f}（16:9=1.778 から逸脱）")
        if w < 1280 or h < 720:
            issues.append(f"解像度 {w}x{h}（最低 1280x720）")
    # 音声 stream チェック
    if not a:
        issues.append("audio stream が無い")
    else:
        codec_a = a.get("codec_name") or ""
        report["audio"] = {"codec": codec_a, "channels": a.get("channels"),
                           "sample_rate": a.get("sample_rate")}
        if codec_a.lower() not in ("aac", "mp3"):
            issues.append(f"audio codec={codec_a}（aac 推奨）")
    # 尺チェック
    dur = report["duration_sec"]
    if dur < 60:
        issues.append(f"尺 {dur:.1f}s が短すぎる（最低 60s）")
    elif dur > 14400:
        issues.append(f"尺 {dur:.1f}s が長すぎる（4 時間超）")

    # オプション: ラウドネス
    if os.environ.get("APP_QA_LOUDNESS", "").strip() in ("1", "true", "yes"):
        if _sh.which("ffmpeg"):
            print(f"  📏 ラウドネス測定中（{dur:.0f}s 動画 → {int(dur/600)+1}分程度）...")
            ln_cmd = [
                "ffmpeg", "-hide_banner", "-nostats", "-i", str(mp4),
                "-af", "loudnorm=print_format=summary",
                "-f", "null", "-",
            ]
            try:
                ln = subprocess.run(ln_cmd, capture_output=True, text=True,
                                    timeout=int(dur * 0.5) + 120)
                m = re.search(r"Input Integrated:\s+(-?\d+\.\d+)\s+LUFS", ln.stderr)
                if m:
                    lufs = float(m.group(1))
                    report["loudness_lufs"] = lufs
                    if abs(lufs - (-16.0)) > 2.0:
                        issues.append(f"ラウドネス {lufs:.1f} LUFS（-16 ±2 LU 推奨から逸脱）")
                else:
                    print("  ⚠ loudnorm summary を解析できず（スキップ）")
            except subprocess.TimeoutExpired:
                print("  ⚠ ラウドネス測定タイムアウト → スキップ")

    report["issues"] = issues
    report["passed"] = len(issues) == 0
    # report 保存
    try:
        (folder / "qa_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"  ⚠ qa_report.json 保存失敗: {e}")

    if not issues:
        print(f"  ✅ QA 合格（{dur:.1f}s {report['video']['width']}x{report['video']['height']} {report['size_mb']}MB）")
        return True
    print(f"  ❌ QA 不合格（{len(issues)} 件）:")
    for it in issues:
        print(f"    - {it}")
    return False


def _build_thumbnail_prompt(folder: Path) -> str:
    """ベンチマーク concept + visual_direction + 動画 concept から英語プロンプトを構築。

    優先順位:
      1. 動画フォルダの concept.txt（per-vol の日本語コンセプト）
      2. benchmark_concept.aggregate.recommendation_for_self.vibe_one_line
      3. benchmark_thumbnail.analysis.aggregate / picked thumbnail elements
      4. competitor_analysis_cache.analysis.visual_direction
      5. fallback: persona
    """
    parts = []
    analysis = {}
    thumbnail_axis = {}
    # 動画固有のコンセプト（最優先）
    concept_file = folder / "concept.txt"
    if concept_file.exists():
        try:
            c = concept_file.read_text(encoding="utf-8").strip()
            if c:
                parts.append(c)
        except Exception:
            pass
    # benchmark concept aggregate
    try:
        sys.path.insert(0, str(BASE))
        import app_benchmark_concept as _bc
        agg = _bc.get_aggregate()
        rec = agg.get("recommendation_for_self") or {}
        vibe = (rec.get("vibe_one_line") or "").strip()
        if vibe:
            parts.append(vibe)
    except Exception:
        pass
    # benchmark thumbnail aggregate
    try:
        import app_benchmark_thumbnail as _bt
        thumb_cache = _bt.load_cache() or {}
        thumbnail_axis = ((thumb_cache.get("analysis") or {}).get("aggregate") or {})
        rec = thumbnail_axis.get("recommendation_for_self") or {}
        vibe = (rec.get("vibe_one_line") or "").strip()
        if vibe:
            parts.append(vibe)
    except Exception:
        thumbnail_axis = {}
    # benchmark visual_direction (competitor cache)
    visual_hint = ""
    try:
        import app_competitor as _ac
        cache = _ac.load_cache() or {}
        analysis = (cache or {}).get("analysis") or {}
        vd = analysis.get("visual_direction") or {}
        tod = (vd.get("time_of_day") or "").strip()
        atm = (vd.get("atmosphere") or "").strip()
        comp = (vd.get("composition") or "").strip()
        palette = vd.get("color_palette") or []
        sub = vd.get("subjects") or []
        avoid = vd.get("avoid") or []
        bits = []
        if tod: bits.append(tod)
        if atm: bits.append(atm)
        if comp: bits.append(comp)
        if palette: bits.append("palette: " + ", ".join(palette[:3]))
        if sub: bits.append("subjects: " + ", ".join(sub[:3]))
        if bits:
            visual_hint = " | ".join(bits)
        if avoid:
            visual_hint += f" | avoid: {', '.join(avoid[:3])}"
    except Exception:
        pass
    if visual_hint:
        parts.append(visual_hint)
    # ペルソナ fallback
    if not parts:
        cfg = _load_dashboard_config()
        persona = (cfg.get("persona") or "").strip()
        if persona:
            parts.append(persona[:200])
    body = ". ".join(parts) if parts else "cinematic atmospheric scene for a BGM YouTube channel"
    try:
        from app_image_prompt import build_gpt_image2_prompt, normalize_visual_direction
        visual = normalize_visual_direction(analysis, thumbnail_axis)
        return build_gpt_image2_prompt(
            concept=body,
            visual_direction=visual,
            for_flow=False,
            include_text_overlay=False,
        )
    except Exception:
        # 1920x1080 / 16:9 制約と「テキスト無し」を末尾に明示
        return (
            f"{body}. Cinematic photorealistic 16:9 thumbnail, "
            "moody lighting, shallow depth of field, no text overlay, no logo, no watermark."
        )


# 背景画像用の禁止語（cozy tune＝カフェの朝。テキスト/ロゴ/人の顔と雑多なゴチャつきは禁止だが、
# カフェ什器〔木製テーブル・コーヒー・観葉植物・窓〕は cozy tune の主役なので禁止しない）
_BGIMAGE_AVOID = (
    "on-screen text, text overlay, captions, subtitles, japanese text, hangul, "
    "korean text, chinese text, lettering, typography, readable text, words, "
    "watermark, signage, logos, readable signage, brand names, human faces, "
    "cluttered still life, decorative ornaments, vases, urns, "
    "dark moody interiors, neon, nightclub lighting, lofi anime illustration style, "
    "dusty grain, heavy vignette, oversaturated pop colors, busy crowds"
)
# 非cozy-tune チャンネル(orzz/WW/HN 等)用の汎用禁止語。テキスト/ロゴ/顔/群衆/lofiアニメ調のみ禁止し、
# dark/night/neon やカフェ固有要素は禁止しない（orzz の NYC 夜ラウンジ・リゾート夕暮れ等を許す）。
_BGIMAGE_AVOID_GENERIC = (
    "on-screen text, text overlay, captions, subtitles, japanese text, hangul, "
    "korean text, chinese text, lettering, typography, readable text, words, "
    "watermark, signage, logos, readable signage, brand names, "
    "prominent human faces, busy crowds, lofi anime illustration style, "
    "dusty grain, heavy vignette, oversaturated pop colors"
)
_BGIMAGE_SCENE = (
    "A bright, airy cozy cafe-morning scene: a clean, modern Japanese/Korean style cafe interior "
    "(or a sunlit window-side nook) with natural wood furniture, lush green houseplants and a touch of "
    "fresh citrus greenery, a cup of coffee with gentle steam, soft daylight pouring through large windows. "
    "If any people appear they are only distant, blurred and faceless. "
    "The feeling is a calm, refined morning of relaxed me-time with a stylish boss-nova / cafe-R&B "
    "playlist mood — clean lifestyle snapshot with large open space reserved for later text design."
)
# 背景画像用ディレクティブ（ループ背景・テキスト無し・被写体控えめ・参照は色/光/雰囲気のみ）
_BGIMAGE_DIRECTIVE = (
    "Primary scene: a cozy, light-filled cafe morning — natural-wood tabletop or window-side nook, "
    "green plants, a coffee cup, bright clean daylight; not a dark lofi room, not a waterfront, not a beach. "
    "Style: warm, natural, bright and clean, atmospheric and calm, suitable for looping as the background "
    "of a multi-hour cafe-BGM video. "
    "Composition: spacious, soft natural side/front light, shallow depth on foreground details, "
    "ample negative space, no readable signage, no busy clutter. "
    "Absolutely NO on-screen text, NO captions, NO subtitles, NO Japanese/Korean/Chinese "
    "characters, NO lettering, NO typography, NO logos, NO watermark anywhere in the image "
    "(text is added later in a separate step; the AI image must contain zero readable text). "
    "Reference images: IGNORE all text, captions, letters, words and logos in them; "
    "use ONLY their color palette, lighting, mood and composition. "
    "Keep the composition original and do not copy any element verbatim and do not reproduce any text."
)


def _legacy_bgimage_prompt(persona: str, channel_name: str) -> str:
    """旧・固定テンプレ背景プロンプト（APP_BGIMAGE_DYNAMIC_PROMPT=0 と動的失敗時のフォールバック）。
    現行 step_bgimage の文言を逐語温存。末尾の ::vol{N}.png は呼び出し側で付与する。"""
    return (
        f"Generate a horizontal 16:9 cinematic background image for a long-form BGM YouTube video"
        f"{' on the channel ' + channel_name if channel_name else ''}. "
        f"Channel concept: {persona} "
        f"Style: atmospheric, calm, suitable for looping as the background of a multi-hour music video. "
        f"Composition: spacious, no on-screen text, no text overlay, no captions, no subtitles, "
        f"no Japanese/Korean/Chinese characters, no lettering, no typography, no readable text, "
        f"no logos, no watermark, no readable signage, no human faces, "
        f"no pottery, no vases, no urns, no planters, no still life objects, no decorative ornaments. "
        f"The AI image must contain zero readable text (text is added later in a separate step). "
        f"If reference images are provided, IGNORE all text, captions, letters and logos in them and "
        f"combine ONLY their color palette, lighting and overall mood "
        f"while making the composition original — do not copy any element verbatim and do not reproduce any text."
    )


def _build_bgimage_prompt(folder: Path, ref_images=None) -> str:
    """背景画像 vol{N}.png 用の動的プロンプトを構築（ベンチマーク concept/visual_direction 由来）。

    収集ロジック（concept.txt → benchmark concept aggregate → benchmark thumbnail aggregate →
    competitor visual_direction → persona fallback）は _build_thumbnail_prompt と同一。
    背景固有の差分: include_text_overlay=False（テキスト無し）、背景禁止語を avoid 先頭に二重注入、
    ループ背景ディレクティブを body 末尾に連結。サムネと違い被写体は控えめ。
    末尾の ::vol{N}.png は呼び出し側で付与する。
    """
    parts = []
    analysis = {}
    thumbnail_axis = {}
    # 動画固有のコンセプト（最優先）
    concept_file = folder / "concept.txt"
    if concept_file.exists():
        try:
            c = concept_file.read_text(encoding="utf-8").strip()
            if c:
                parts.append(c)
        except Exception:
            pass
    # benchmark concept aggregate
    try:
        sys.path.insert(0, str(BASE))
        import app_benchmark_concept as _bc
        agg = _bc.get_aggregate()
        rec = agg.get("recommendation_for_self") or {}
        vibe = (rec.get("vibe_one_line") or "").strip()
        if vibe:
            parts.append(vibe)
    except Exception:
        pass
    # benchmark thumbnail aggregate
    try:
        import app_benchmark_thumbnail as _bt
        thumb_cache = _bt.load_cache() or {}
        thumbnail_axis = ((thumb_cache.get("analysis") or {}).get("aggregate") or {})
        rec = thumbnail_axis.get("recommendation_for_self") or {}
        vibe = (rec.get("vibe_one_line") or "").strip()
        if vibe:
            parts.append(vibe)
    except Exception:
        thumbnail_axis = {}
    # benchmark visual_direction (competitor cache)
    visual_hint = ""
    try:
        import app_competitor as _ac
        cache = _ac.load_cache() or {}
        analysis = (cache or {}).get("analysis") or {}
        vd = analysis.get("visual_direction") or {}
        tod = (vd.get("time_of_day") or "").strip()
        atm = (vd.get("atmosphere") or "").strip()
        comp = (vd.get("composition") or "").strip()
        palette = vd.get("color_palette") or []
        avoid = vd.get("avoid") or []
        bits = []
        if tod: bits.append(tod)
        if atm: bits.append(atm)
        if comp: bits.append(comp)
        if palette: bits.append("palette: " + ", ".join(palette[:3]))
        # ⚠ 背景画像は被写体を強調しない → サムネと違い subjects は載せない
        if bits:
            visual_hint = " | ".join(bits)
        if avoid:
            visual_hint += f" | avoid: {', '.join(avoid[:3])}"
    except Exception:
        pass
    if visual_hint:
        parts.append(visual_hint)
    # ペルソナ fallback
    cfg = _load_dashboard_config()
    persona = (cfg.get("persona") or "").strip()
    if not parts and persona:
        parts.append(persona[:200])
    context = ". ".join(parts) if parts else "cinematic atmospheric scene for a BGM YouTube channel"
    # ⚠ cozy-tune チャンネル(SUKIMA)のみ cozy-cafe シーンを主題に固定。それ以外(orzz/WW/HN)は
    #    context(concept.txt + benchmark visual_direction + persona)を主題に使う。
    #    （旧コードは全chで cozy-cafe を主題にしていたため orzz が「カフェの朝」になっていた）
    cozy_tune = bool(cfg.get("bgimage_cozy_tune", False))
    custom_style = (cfg.get("bgimage_style") or "").strip()  # 非cozy時の独自画風(油彩/イラスト調 等)
    if cozy_tune:
        body = f"{_BGIMAGE_SCENE} Channel mood context, do not depict literally: {context}. {_BGIMAGE_DIRECTIVE}"
        avoid_const = _BGIMAGE_AVOID
    else:
        # per-ch `bgimage_style` で画風を上書き可能（未設定は写実）。例: 油彩/グワッシュ調のおしゃれイラスト。
        style = custom_style or "photorealistic, cinematic"
        body = (
            f"Primary scene: {context}. "
            f"{style} looping background for a multi-hour BGM video. "
            "Spacious composition with ample negative space and soft lighting. "
            "No readable text, no captions, no logos, no watermark."
        )
        avoid_const = _BGIMAGE_AVOID_GENERIC
    try:
        from app_image_prompt import build_gpt_image2_prompt, normalize_visual_direction
        visual = normalize_visual_direction(analysis, thumbnail_axis)
        # 独自画風(bgimage_style)を Style/rendering 行へ＝build_gpt_image2_prompt の
        # photorealistic 強制も解除される（Subject だけに置くと写実制約に負けるため）。
        if custom_style and not cozy_tune:
            visual["style"] = custom_style
        # ⚠ 旧コードは background_context を「cozy bright cafe morning...」でハードコード固定し、
        #    海/川/浜辺まで禁止していた（vol がカフェ朝ばかりになった元凶）。撤去し、
        #    今回選んだ参照3枚の Vision 共通要素に置換する＝ベンチ先サムネ3枚に寄せる本来仕様(TTPS)。
        if ref_images:
            try:
                from app_llm_runner import run_llm_vision
                _common = run_llm_vision(
                    "これら複数のベンチマーク用サムネ画像に共通する視覚要素"
                    "（被写体・構図・時間帯・光・色調・雰囲気）を分析し、それらを踏襲した"
                    "新しい16:9背景画像のための英語ビジュアル記述を1〜2文で作れ。"
                    "文字/ロゴ/固有名詞は含めない。被写体は控えめ・余白多め。",
                    [str(p) for p in ref_images],
                    label="bgimage:参照3枚の共通要素抽出",
                )
                if _common and _common.strip():
                    visual["background_context"] = _common.strip()
                    print("  🔍 参照3枚のVision共通要素を背景コンテキストに採用(TTPS)")
            except Exception as e:
                print(f"  ⚠ 参照3枚のVision共通要素抽出失敗（ベンチ分析にフォールバック）: {e}")
        visual["camera_composition"] = (
            "16:9 wide landscape shot, soft natural light, "
            "spacious airy composition with ample negative space"
        )
        # 背景禁止語を avoid の先頭に二重注入（_clean_text の 220 字切りで消えないよう先頭固定）
        existing_avoid = (visual.get("avoid") or "").strip()
        visual["avoid"] = (avoid_const + ("; " + existing_avoid if existing_avoid else ""))
        return build_gpt_image2_prompt(
            concept=body,
            visual_direction=visual,
            for_flow=False,
            include_text_overlay=False,
        )
    except Exception:
        # ビルダー import 失敗時は body + 背景禁止語 + 16:9/no text の固定文を返す
        return (
            f"{body}. Cinematic photorealistic 16:9 background, looping-friendly, "
            f"no text overlay, no logo, no watermark. Avoid: {avoid_const}."
        )


def _eval_loop_env_int(name: str, default: int) -> int:
    try:
        v = (os.environ.get(name) or "").strip()
        return int(v) if v else default
    except (TypeError, ValueError):
        return default


def _eval_loop_env_float(name: str, default: float) -> float:
    try:
        v = (os.environ.get(name) or "").strip()
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


def _run_thumbnail_eval_loop(vol: int, folder: Path, out_dir: Path,
                             final_path: Path, prompt_base: str):
    """D5: 自己評価付き反復サムネ生成ループ（APP_THUMB_EVAL_LOOP=1 時のみ）。

    既存の codex 生成を generate_fn、score_thumbnails を score_fn に包んで
    app_image_eval_loop.run_eval_loop に注入する。合格画像（無ければ best）を
    thumbnail.png に昇格する。

    戻り:
      True / False = ループは走った（昇格できたか否か。いずれも upload は止めない）
      None         = ループ自体が使えない（モジュール import 失敗等）→ 呼び出し側で従来生成にフォールバック
    """
    try:
        from app_image_eval_loop import run_eval_loop, GenResult, ScoreResult
        from app_thumbnail_scoring import (
            score_thumbnails, judge_auto_approval, status_counts, best_of,
        )
    except Exception as e:
        print(f"  ⚠ eval-loop モジュール import 失敗: {e}")
        return None

    cfg = _load_dashboard_config()
    persona = (cfg.get("persona") or "").strip()
    channel_name = (cfg.get("channel_name") or "").strip()
    cli = _load_suno_config().get("claude_cli", "claude")

    # 採点用の文脈（title=youtube_title.txt → fallback folder 名 / concept=concept.txt）
    video_title = ""
    title_file = folder / "youtube_title.txt"
    if title_file.exists():
        try:
            lines = [ln for ln in title_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            video_title = lines[0].strip() if lines else ""
        except Exception:
            video_title = ""
    if not video_title:
        video_title = folder.name
    video_concept = ""
    concept_file = folder / "concept.txt"
    if concept_file.exists():
        try:
            video_concept = concept_file.read_text(encoding="utf-8").strip()
        except Exception:
            video_concept = ""

    # 競合参照（picked サムネ）と生成参照（先頭1枚）
    ref_paths: list = []
    picked_ref = None
    thumbnail_axis: dict = {}
    try:
        import app_benchmark_thumbnail as _bt
        picked = _bt.get_picked_paths(limit=4) or []
        ref_paths = [Path(p) for p in picked if Path(p).exists()]
        if ref_paths:
            picked_ref = str(ref_paths[0])
        tc = _bt.load_cache() or {}
        thumbnail_axis = ((tc.get("analysis") or {}).get("aggregate") or {}) or {}
    except Exception as e:
        print(f"  ⚠ eval-loop 競合参照/軸の取得失敗（参照なしで継続）: {e}")

    n_per = _eval_loop_env_int("APP_THUMB_N_PER_ATTEMPT", 4)
    codex_model = os.environ.get("APP_THUMBNAIL_IMAGE_MODEL", "gpt-image-2")
    codex_size = os.environ.get("APP_THUMBNAIL_IMAGE_SIZE", "1536x1024")
    codex_quality = os.environ.get("APP_THUMBNAIL_IMAGE_QUALITY", "medium")
    codex_parallel = (os.environ.get("APP_THUMBNAIL_CODEX_MAX_PARALLEL") or str(n_per))

    def _glob_names() -> set:
        names = set()
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            for p in out_dir.glob(ext):
                names.add(p.name)
        return names

    def generate_fn(concept_hint: str, axis: dict, attempt: int):
        body = concept_hint or prompt_base
        avoid = list((axis or {}).get("avoid") or [])
        if avoid:
            body = f"{body} Avoid: {', '.join(str(a) for a in avoid[:5])}."
        prompt_text = f"{body}::vol{vol}_thumb_a{attempt}"
        before = _glob_names()
        codex_cmd = [
            sys.executable, "-u", str(BASE / "codex_imagegen.py"),
            "--output-dir", str(out_dir),
            "--max-parallel", codex_parallel,
            "--model", codex_model, "--size", codex_size, "--quality", codex_quality,
            "--n", str(n_per),
            "--prompt", prompt_text,
        ]
        if picked_ref:
            codex_cmd += ["--reference-image", picked_ref]
        try:
            p = subprocess.Popen(codex_cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
            stdout, _ = p.communicate(timeout=900)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            try:
                p.terminate()
            except Exception:
                pass
            return GenResult([], infra_error="timeout")
        except Exception as e:
            return GenResult([], infra_error=f"launch:{type(e).__name__}")
        new_files = []
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            new_files.extend(p2 for p2 in out_dir.glob(ext) if p2.name not in before)
        new_files.sort(key=lambda p2: p2.stat().st_mtime)
        if not new_files:
            low = (stdout or "").lower()
            if "quota" in low or "rate limit" in low or "insufficient_quota" in low:
                return GenResult([], infra_error="quota")
            if "unauthor" in low or " 401" in low or "auth" in low:
                return GenResult([], infra_error="auth")
            return GenResult([], infra_error=f"exit={rc}")
        return GenResult(new_files)

    def score_fn(files: list):
        try:
            sc = score_thumbnails(
                generated_paths=list(files),
                competitor_paths=ref_paths[:4],
                video_title=video_title,
                video_concept=video_concept,
                channel_name=channel_name,
                persona=persona,
                cli_cmd=cli,
                timeout=600,
            )
        except Exception as e:
            return ScoreResult([], error=f"{type(e).__name__}: {str(e)[:160]}")
        return ScoreResult(sc.get("evaluations") or [], error=sc.get("error") or "")

    max_attempts = _eval_loop_env_int("APP_THUMB_MAX_ATTEMPTS", 2)
    print(f"  🔁 eval-loop 開始（max_attempts={max_attempts}, n/attempt={n_per}, "
          f"ref={len(ref_paths)} 枚, picked_ref={'有' if picked_ref else '無'}）")
    res = run_eval_loop(
        generate_fn=generate_fn, score_fn=score_fn,
        judge_fn=judge_auto_approval, status_counts_fn=status_counts, best_of_fn=best_of,
        concept_hint_base=prompt_base, thumbnail_axis=thumbnail_axis,
        score_threshold=_eval_loop_env_int("APP_THUMB_SCORE_THRESHOLD", 80),
        ctr_threshold=_eval_loop_env_float("APP_THUMB_CTR_THRESHOLD", 7.0),
        similarity_max=_eval_loop_env_int("APP_THUMB_SIMILARITY_MAX", 25),
        required_pass=_eval_loop_env_int("APP_THUMB_REQUIRED_PASS", 1),
        max_attempts=max_attempts,
        max_total_generated=_eval_loop_env_int("APP_THUMB_MAX_TOTAL_GEN", 8),
        max_vision_calls=_eval_loop_env_int("APP_THUMB_MAX_VISION_CALLS", 3),
        log_fn=print,
    )

    # 採用判定: 合格なら approved（その中の best）、不合格は ADOPT フラグ次第で best を採用
    adopt_on_fail = (os.environ.get("APP_THUMB_EVAL_ON_FAIL_ADOPT") or "").strip() in ("1", "true", "yes")
    chosen_name = None
    if res.passed:
        approved_set = set(res.approved_files)
        approved_evals = [e for e in res.all_evaluations if e.get("filename") in approved_set]
        pick = best_of(approved_evals) or res.best
        chosen_name = (pick or {}).get("filename")
    elif adopt_on_fail and res.best:
        chosen_name = res.best.get("filename")
        print("  ⚠ eval-loop 不合格だが APP_THUMB_EVAL_ON_FAIL_ADOPT=1 → best を採用")

    if not chosen_name:
        print(f"  ⚠ eval-loop: 昇格画像なし（passed={res.passed}, abort={res.abort_reason!r}）"
              " → thumbnail.png 未設定（候補は thumbnail_candidates に保管・手動承認可）")
        return False

    src = out_dir / chosen_name
    if not src.exists():
        matches = [p for p in out_dir.glob("*") if p.name == chosen_name]
        src = matches[0] if matches else None
    if not src or not src.exists():
        print(f"  ⚠ eval-loop: 採用ファイル {chosen_name} が見つからず → 未昇格")
        return False
    try:
        import shutil as _sh
        _sh.copy2(src, final_path)
        tag = "合格" if res.passed else "best(fail-adopt)"
        print(f"  ✅ eval-loop: {src.name} → {final_path.name}（{tag}・"
              f"attempts={res.attempts_used}, gen={res.total_generated}, vision={res.vision_calls}）")
    except Exception as e:
        print(f"  ⚠ eval-loop: thumbnail.png コピー失敗: {e}")
        return False
    return True


def step_thumbnail(vol: int, folder: Path, via_api: bool, **kw):
    """サムネ自動生成（P2-5）。Codex（gpt-image）でプロンプト生成。

    出力先:
      <vol_folder>/thumbnail_candidates/codex_*.png
      → 先頭 1 枚を <vol_folder>/thumbnail.png に昇格

    既存の thumbnail.png（手動配置 or vol*.jpg）があればスキップ。

    プロバイダ選択（自動サムネ生成は codex 一本化。Flow は削除済み）:
      env APP_THUMBNAIL_PROVIDERS=codex        → Codex のみ（既定）
      env APP_THUMBNAIL_PROVIDERS=flow         → 警告のうえ codex にフォールバック
      env APP_THUMBNAIL_DISABLE=1              → step 全体スキップ

    失敗は upload を止めない（最終的な thumbnail.png が無くても upload は続行可能）。"""
    print(f"\n{'='*60}")
    print(f"  {STEP_LABELS.get('thumbnail', '5.5/7 サムネイル自動生成')}")
    print(f"{'='*60}")
    if os.environ.get("APP_THUMBNAIL_DISABLE", "").strip() in ("1", "true", "yes"):
        print("  ⏭ APP_THUMBNAIL_DISABLE=1 によりスキップ")
        return True
    # 既存のサムネがあればスキップ
    final_path = folder / "thumbnail.png"
    existing = final_path.exists() or any(folder.glob("vol*.jpg")) or (folder / "サムネイル.jpg").exists()
    if existing:
        print(f"  ⏭ 既存サムネあり → スキップ ({final_path if final_path.exists() else next(folder.glob('vol*.jpg'), None) or folder/'サムネイル.jpg'})")
        return True

    providers_raw = (os.environ.get("APP_THUMBNAIL_PROVIDERS") or "codex").lower()
    raw_list = [p.strip() for p in providers_raw.split(",") if p.strip()]
    if "flow" in raw_list:
        print("  ⚠ Flow は削除済み → codex で生成します")
    # Flow は除外。残るは codex のみ（自動サムネ生成は codex 一本化）
    providers = [p for p in raw_list if p == "codex"]
    if not providers:
        # raw に flow しか無かった場合は codex にフォールバック、完全に空なら従来どおりスキップ
        if "flow" in raw_list:
            providers = ["codex"]
        else:
            print(f"  ⚠ APP_THUMBNAIL_PROVIDERS={providers_raw!r} が無効 → step スキップ")
            return True

    prompt = _build_thumbnail_prompt(folder)
    print(f"  📝 prompt: {prompt[:180]}{'…' if len(prompt) > 180 else ''}")
    out_dir = folder / "thumbnail_candidates"
    out_dir.mkdir(parents=True, exist_ok=True)

    # D5: 自己評価付き反復生成ループ（既定OFF・APP_THUMB_EVAL_LOOP=1 でオプトイン）。
    # ON 時は生成→Vision採点→合格まで再生成＋弱点還流。OFF 時は従来の単発生成（下記）と完全一致。
    if (os.environ.get("APP_THUMB_EVAL_LOOP") or "").strip() in ("1", "true", "yes"):
        looped = _run_thumbnail_eval_loop(vol, folder, out_dir, final_path, prompt)
        if looped is not None:
            # ループが走った（昇格できなくても upload は止めない＝従来の non-fatal 方針）
            return True
        print("  ⚠ eval-loop 利用不可 → 従来の単発生成にフォールバック")

    procs: list = []
    # Codex（API 並列・OK）— 自動サムネ生成は codex 一本化（Flow は削除済み）
    if "codex" in providers:
        codex_max_parallel = os.environ.get("APP_THUMBNAIL_CODEX_MAX_PARALLEL", "1")
        codex_cmd = [
            sys.executable, "-u", str(BASE / "codex_imagegen.py"),
            "--output-dir", str(out_dir),
            "--max-parallel", codex_max_parallel,
            "--model", os.environ.get("APP_THUMBNAIL_IMAGE_MODEL", "gpt-image-2"),
            "--size", os.environ.get("APP_THUMBNAIL_IMAGE_SIZE", "1536x1024"),
            "--quality", os.environ.get("APP_THUMBNAIL_IMAGE_QUALITY", "medium"),
            "--prompt", f"{prompt}::vol{vol}_thumb",
        ]
        try:
            import app_benchmark_thumbnail as _bt
            picked_paths = _bt.get_picked_paths(limit=1)
            if picked_paths:
                codex_cmd += ["--reference-image", picked_paths[0]]
                print(f"  📎 codex reference: {Path(picked_paths[0]).name}")
        except Exception as e:
            print(f"  ⚠ codex picked 参照失敗: {e}")
        try:
            p = subprocess.Popen(codex_cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
            procs.append(("codex", p))
            print(f"  🚀 codex_imagegen.py を起動 (pid={p.pid})")
        except Exception as e:
            print(f"  ❌ Codex 起動失敗: {e}")

    # 起動した生成プロセスの終了を待つ（タイムアウト 600s）
    results = {}
    for name, p in procs:
        try:
            stdout, _ = p.communicate(timeout=600)
            results[name] = (p.returncode, (stdout or "")[-300:])
            tag = "✅" if p.returncode == 0 else "❌"
            print(f"  {tag} {name}: exit={p.returncode}")
        except subprocess.TimeoutExpired:
            p.terminate()
            results[name] = (-1, "timeout")
            print(f"  ⏰ {name}: timeout 600s")

    # 候補を thumbnail.png に昇格（先頭 1 枚を選ぶ。codex 生成物のみ）
    candidates = []
    for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
        candidates.extend(out_dir.glob(ext))
    if not candidates:
        print(f"  ⚠ サムネ候補が生成されませんでした（results={results}）")
        # upload を止めない
        return True
    # codex 一本化のため優先順位は不要。最も古い生成物を先頭に（mtime 昇順）
    candidates.sort(key=lambda p: p.stat().st_mtime)
    chosen = candidates[0]
    try:
        import shutil as _sh
        _sh.copy2(chosen, final_path)
        print(f"  ✅ {chosen.name} → {final_path.name}（残り {len(candidates) - 1} 件は thumbnail_candidates に保管）")
    except Exception as e:
        print(f"  ⚠ thumbnail.png コピー失敗: {e}")
    return True


def step_upload(vol: int, folder: Path, via_api: bool, **kw):
    privacy = kw.get("privacy", "unlisted")
    # P3-3: per-channel `publish_mode`（公開方式＝予約投稿イメージ）で upload privacy と
    # 公開ゲートを決める。
    #   - "unlisted" : 限定公開で upload、公開ゲート無し（既定・現行動作）
    #   - "public"   : 即時 public で upload
    #   - "delayed"  : private で upload → `publish_delay_hours` 時間後に自動 public 化
    # 後方互換: publish_mode 未設定なら publish_delay_hours>0 → "delayed"、それ以外 "unlisted"。
    cfg = _load_dashboard_config()
    try:
        delay_h = float(cfg.get("publish_delay_hours") or 0)
    except (TypeError, ValueError):
        delay_h = 0.0
    mode = (cfg.get("publish_mode") or "").strip().lower()
    if mode not in ("unlisted", "public", "delayed"):
        mode = "delayed" if delay_h > 0 else "unlisted"

    schedule_gate = False
    if mode == "public":
        privacy = "public"
        print("  🌐 publish_mode=public → 即時公開で upload")
    elif mode == "delayed" and delay_h > 0:
        privacy = "private"
        schedule_gate = True
        print(f"  🔒 publish_mode=delayed → upload は private、{delay_h}h 後に自動 public 化")
    elif mode == "delayed" and delay_h <= 0:
        # delayed なのに遅延時間が無い → 即時 public とみなす（設定の取りこぼし救済）
        privacy = "public"
        print("  🌐 publish_mode=delayed だが publish_delay_hours=0 → 即時公開で upload")
    else:
        print(f"  🔗 publish_mode=unlisted → 限定公開で upload（公開ゲート無し）")

    if via_api:
        r = _api_post("/api/youtube/upload", {
            "video_name": folder.name, "privacy": privacy,
        }, "YouTube アップロード")
        if not r:
            return False
        ok = _api_poll("/api/youtube/status", "YouTube アップロード", timeout=7200)
        if ok and schedule_gate:
            _schedule_publish_after_upload(folder, delay_h)
        return ok
    else:
        ok = _run([
            sys.executable, str(BASE / "app_youtube.py"),
            str(folder), "--privacy", privacy,
        ], STEP_LABELS["upload"], timeout=7200)
        if ok is True and schedule_gate:
            _schedule_publish_after_upload(folder, delay_h)
        return ok


def _schedule_publish_after_upload(folder: Path, delay_hours: float) -> None:
    """upload 成功直後に /api/youtube/schedule-publish を叩いて公開ゲートを登録。

    API が落ちている場合は warn のみ（次回 app.py 起動時の startup hook で復旧される）。
    """
    cfg = _load_dashboard_config()
    body = {
        "video_name": folder.name,
        "channel_folder": str(folder.parent),
        "channel_name": cfg.get("channel_name") or "",
        "delay_hours": float(delay_hours),
    }
    try:
        r = _api_post("/api/youtube/schedule-publish", body, "公開ゲート登録")
        if r and r.get("status") == "ok":
            print(f"  ⏰ 公開予定: {r.get('scheduled_at')} ({delay_hours}h 後)")
        else:
            print(f"  ⚠ 公開ゲート登録の応答が不正（marker は更新済 → 次回起動で復旧）")
    except Exception as e:
        print(f"  ⚠ 公開ゲート登録失敗: {e}（marker は更新済）")


STEP_FUNCS = {
    "plan": step_plan,
    "suno": step_suno,
    "rename": step_rename,
    "bgimage": step_bgimage,
    "psd_composite": step_psd_composite,
    "premiere": step_premiere,
    "export": step_export,
    "qa": step_qa,
    "meta": step_meta,
    "localization": step_localization,
    "thumbnail": step_thumbnail,
    "upload": step_upload,
}


# ─── stage 単位 retry/backoff レイヤ（P1-2） ───
# 各 stage 関数は True / False / "unattended_login" / "retryable" を返す前提。
# - "unattended_login" は **絶対に retry しない**（手動ログイン要 → Discord 通知）。
# - その他の失敗は stage ごとの policy に従って指数バックオフで再投入。
#
# attempts: 最大試行回数（1 = 再試行なし）
# backoff:  N 回目の失敗後に sleep する秒数のリスト。len < attempts-1 の時は最後の値を流用。
# retry_on: 再試行する戻り値の集合。空集合なら fast-fail。
RETRY_POLICY: dict = {
    # plan / meta は API 呼び出し中心で transient 失敗が多い → 軽く 2 回まで
    "plan": {"attempts": 2, "backoff": [15], "retry_on": {False}},
    "meta": {"attempts": 2, "backoff": [10], "retry_on": {False}},
    "localization": {"attempts": 2, "backoff": [15], "retry_on": {False}},
    # upload は明示的に EXIT_RETRYABLE で投げ返ってきた時のみリトライ。
    # 一般 False（ログイン切れ・mp4 不在等）はリトライしても直らないので fast-fail。
    "upload": {"attempts": 3, "backoff": [30, 120], "retry_on": {"retryable"}},
    # suno は内部に copyright 3x retry を持つので外側はリトライしない
    # premiere / export / rename は決定論的 or 物理制約 → リトライ無意味
    # 上記以外は attempts=1（DEFAULT_POLICY）
}
DEFAULT_POLICY = {"attempts": 1, "backoff": [], "retry_on": set()}

# 失敗時、その工程を再実行しても直らず「前段からやり直す」べき工程の差し戻し先。
# 例: QA は mp4 を ffprobe で検査するだけ → NG なら mp4 自体が不良なので
#     qa を再実行しても同じ結果。Premiere から再レンダーして初めて直る。
# pipeline は失敗時に末尾へ「再開: ... --from <stage>」を出力し、
# auto_resume(app.py) がそれを解析して再投入する。ここで差し戻し先を上書きする。
_RESUME_OVERRIDE = {
    "qa": "premiere",
}


def _run_step_with_retry(name: str, step_func, *args, **kw):
    """単一 stage を policy に従って retry しつつ実行。

    短絡条件:
      - True → そのまま返す（成功）
      - "unattended_login" → 即座に返す（手動対応必須）
      - 戻り値が retry_on に無い → 即座に返す
      - attempts 上限に到達 → 最後の戻り値を返す
    """
    policy = RETRY_POLICY.get(name, DEFAULT_POLICY)
    attempts = max(1, int(policy.get("attempts", 1)))
    backoff = list(policy.get("backoff", []))
    retry_on = set(policy.get("retry_on", set()))
    # 環境変数で全 retry を無効化（開発時の早期失敗用）
    if os.environ.get("APP_RETRY_DISABLE", "").strip() in ("1", "true", "yes"):
        attempts = 1

    result = None
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            wait = backoff[min(attempt - 2, len(backoff) - 1)] if backoff else 60
            print(f"\n  ↻ {name}: retry {attempt}/{attempts}（前回={result!r}, {wait}s 後に再試行）")
            sys.stdout.flush()
            time.sleep(wait)
        result = step_func(*args, **kw)
        if result is True:
            return True
        if result == "unattended_login":
            return result
        if result not in retry_on:
            return result
    # retry を使い切った
    print(f"\n  ⛔ {name}: retry 上限 {attempts} 回に到達。最後の結果={result!r}")
    sys.stdout.flush()
    return result


# ─── メイン ───

def main():
    parser = argparse.ArgumentParser(
        description="orzz. 一括パイプライン: 6 工程を順に実行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
工程:
  suno      SUNO 楽曲生成（Workspace 作成 + batch モード）
  rename    楽曲リネーム + FFmpeg 後処理
  premiere  Premiere 自動配置（.prproj 自動オープン）
  export    Media Encoder 書き出し
  meta      Claude CLI でタイトル・説明・タグ生成
  upload    YouTube アップロード

例:
  python3 app_pipeline.py 78                    # vol.78 を全工程
  python3 app_pipeline.py 78 --from premiere    # Premiere から
  python3 app_pipeline.py 78 --only meta        # メタ生成だけ
  python3 app_pipeline.py 78 --via-api          # Web API 経由
  python3 app_pipeline.py 78 --dry-run          # 確認のみ
""",
    )
    parser.add_argument("vol", type=int, help="vol 番号（例: 78）")
    parser.add_argument("--from", dest="from_step", choices=STEPS_WITH_PLAN, help="指定工程から開始")
    parser.add_argument("--only", choices=STEPS_WITH_PLAN, help="指定工程だけ実行")
    parser.add_argument("--via-api", action="store_true", help="Web API (localhost:8888) 経由で実行")
    parser.add_argument("--dry-run", action="store_true", help="実行せず計画だけ表示")
    parser.add_argument("--duration", type=int, default=None, help="Premiere 目標時間（秒）。未指定なら APP_DURATION_SEC env → per-channel default_duration_sec → 10800 の順で解決")
    parser.add_argument("--suno-count", type=int, default=None, help="SUNO 生成回数")
    parser.add_argument("--suno-interval", type=int, default=None, help="SUNO ループ間隔（秒）")
    parser.add_argument("--privacy", default="unlisted", choices=["private", "unlisted", "public"])
    parser.add_argument("--from-benchmark", action="store_true",
                        help="先頭に plan ステップを追加（ベンチマーク分析から SUNO プロンプトを生成）")
    parser.add_argument("--channel-folder", default=None,
                        help="このジョブで使うチャンネルフォルダ（パス）。"
                             "指定すると UI のアクティブチャンネル設定 (dashboard_config.json) を"
                             "上書き読みする（複数チャンネル並列ジョブ向け）。"
                             "環境変数 APP_CHANNEL_FOLDER でも同等。")
    parser.add_argument("--channel-id", default=None,
                        help="--channel-folder の代わりに channel_id（registry の id 列）で指定。"
                             "channels.json に登録されているチャンネルから folder を解決。"
                             "環境変数 APP_CHANNEL_ID でも同等。")
    parser.add_argument("--auto", action="store_true",
                        help="無人モード: 失敗時に確認プロンプトを出さず即終了。"
                             "APP_NO_INTERACTIVE=1 と等価。")
    args = parser.parse_args()

    # --channel-id / --channel-folder / --auto を環境変数に昇格して子プロセスに伝播させる
    # 優先度: --channel-id (registry 経由) > --channel-folder > APP_CHANNEL_ID env > APP_CHANNEL_FOLDER env > UI active
    resolved_ch = {}
    if args.channel_id:
        resolved_ch = _resolve_channel(channel_id=args.channel_id)
        if not resolved_ch:
            print(f"❌ --channel-id がチャンネル registry (channels.json) に存在しません: {args.channel_id}")
            sys.exit(2)
    elif args.channel_folder:
        ch_path = Path(args.channel_folder).expanduser()
        if not ch_path.exists():
            print(f"❌ --channel-folder が指していないフォルダ: {ch_path}")
            sys.exit(2)
        # registry に登録されているか確認（warn のみ。未登録でも実行は許可）
        resolved_ch = _resolve_channel(channel_folder=str(ch_path))
        if not resolved_ch:
            print(f"  ⚠ --channel-folder が channels.json に未登録: {ch_path}")
            resolved_ch = {"folder": str(ch_path), "name": ch_path.name, "id": ""}
    elif os.environ.get("APP_CHANNEL_ID"):
        # subprocess 起動時に env だけで来るケース
        resolved_ch = _resolve_channel(channel_id=os.environ["APP_CHANNEL_ID"])

    if resolved_ch:
        ch_path = Path(resolved_ch["folder"]).expanduser()
        os.environ["APP_CHANNEL_FOLDER"] = str(ch_path)
        os.environ["APP_CHANNEL_NAME"] = resolved_ch.get("name") or ch_path.name
        if resolved_ch.get("id"):
            os.environ["APP_CHANNEL_ID"] = resolved_ch["id"]
        print(f"  📌 channel: id={resolved_ch.get('id') or '(unregistered)'} "
              f"name={os.environ['APP_CHANNEL_NAME']} folder={ch_path}")
    if args.auto:
        os.environ["APP_NO_INTERACTIVE"] = "1"

    # P2-2: 無人モードで明示的なチャンネル指定が無い場合は警告。
    # 並列実行時のチャンネル取り違えを防ぐため、スケジューラ起動には常に
    # --channel-id か --channel-folder（or env 経由）を渡すべき。
    if args.auto and not (
        args.channel_id or args.channel_folder
        or os.environ.get("APP_CHANNEL_ID") or os.environ.get("APP_CHANNEL_FOLDER")
    ):
        print("  ⚠ --auto モードでチャンネル指定がありません。"
              "UI active channel にフォールバックします。"
              "並列実行時は --channel-id を明示してください。")

    # フォルダ解決
    folder = _resolve_folder(args.vol)
    if not folder:
        print(f"❌ vol.{args.vol} のフォルダが見つかりません")
        print(f"   「python3 app_pipeline.py {args.vol}」の前にフォルダを作成してください")
        sys.exit(1)

    print("=" * 60)
    print(f"  orzz. パイプライン: vol.{args.vol}")
    print(f"  フォルダ: {folder}")
    print("=" * 60)

    # 実行する工程を決定
    base_steps = STEPS_WITH_PLAN if args.from_benchmark else STEPS
    env_steps = (os.environ.get("APP_PIPELINE_STEPS") or os.environ.get("ORZZ_PIPELINE_STEPS", ""))
    if env_steps:
        steps = [s.strip() for s in env_steps.split(",") if s.strip() in STEPS_WITH_PLAN]
    elif args.only:
        steps = [args.only]
    elif args.from_step:
        idx = base_steps.index(args.from_step) if args.from_step in base_steps else 0
        steps = base_steps[idx:]
    else:
        steps = base_steps[:]

    # 計画表示
    print("\n実行計画:")
    for i, s in enumerate(steps, 1):
        print(f"  [{i}] {STEP_LABELS[s]}")
    print()

    if args.dry_run:
        print("(dry-run) ここで終了します。")
        return

    # ─── preflight チェック（Premiere/Export 工程を含む場合のみ） ───
    # 起動していない Premiere に向けて長時間 SUNO を回した後で気付く事故を防ぐ。
    # APP_PREFLIGHT_DISABLE=1 で抑止可能（CEP 不要なテスト時など）。
    needs_premiere = any(s in ("premiere", "export") for s in steps)
    if needs_premiere and os.environ.get("APP_PREFLIGHT_DISABLE", "").strip() not in ("1", "true", "yes"):
        ok, msg = _preflight_premiere()
        if not ok:
            print(f"\n❌ preflight: {msg}")
            ch_name = _load_dashboard_config().get("channel_name", "(unknown channel)")
            _notify_line(
                f"⚠️ [{ch_name}] vol.{args.vol} preflight 失敗\n"
                f"原因: {msg}\n"
                f"対応: Premiere Pro を起動し「ウィンドウ > 拡張機能 > Premiere Link」を開いてから再開: "
                f"python3 app_pipeline.py {args.vol}"
            )
            print(f"\n{'='*60}")
            print(f"  ⛔ preflight 失敗のため pipeline を開始しません")
            print(f"{'='*60}")
            sys.exit(EXIT_PREFLIGHT_FAIL)
        print(f"\n✅ preflight: {msg}")

    # 各工程を順に実行
    # ポジショナル引数として渡すキー（vol / folder / via_api）は kw から除外して
    # TypeError: got multiple values for argument 'vol' を防ぐ
    _POSITIONAL_KEYS = {"vol", "folder", "via_api"}
    kw = {k: v for k, v in vars(args).items() if v is not None and k not in _POSITIONAL_KEYS}

    for s in steps:
        idx = steps.index(s) + 1
        print(f"\n▶ STEP {s} 開始 ({idx}/{len(steps)}) — {STEP_LABELS[s]}")
        sys.stdout.flush()
        func = STEP_FUNCS[s]
        try:
            ok = _run_step_with_retry(s, func, args.vol, folder, args.via_api, **kw)
        except Exception as e:
            print(f"◀ STEP {s} 結果: 例外 — {type(e).__name__}: {e}")
            sys.stdout.flush()
            raise
        # unattended_login は専用ハンドリング（Discord で「ログインが必要」と通知）
        if ok == "unattended_login":
            print(f"◀ STEP {s} 結果: UNATTENDED_LOGIN_REQUIRED")
            sys.stdout.flush()
            ch_name = _load_dashboard_config().get("channel_name", "(unknown channel)")
            _notify_line(
                f"⚠️ [{ch_name}] vol.{args.vol} の {STEP_LABELS[s]} が中断しました\n"
                f"原因: ブラウザの手動ログインが必要 (~/.flow-playwright-profile が切れている可能性)\n"
                f"対応: 該当サービスにブラウザでログインし、再開: python3 app_pipeline.py {args.vol} --from {s}"
            )
            print(f"\n{'='*60}")
            print(f"  🔐 {STEP_LABELS[s]} で中断しました（手動ログイン要求）")
            print(f"  再開: python3 app_pipeline.py {args.vol} --from {s}")
            print(f"{'='*60}")
            sys.exit(EXIT_UNATTENDED)
        # quota_exhausted は専用ハンドリング（24h 待機が必要、retry 無意味）
        if ok == "quota_exhausted":
            print(f"◀ STEP {s} 結果: QUOTA_EXHAUSTED")
            sys.stdout.flush()
            ch_name = _load_dashboard_config().get("channel_name", "(unknown channel)")
            _notify_line(
                f"📊 [{ch_name}] vol.{args.vol} の {STEP_LABELS[s]} が中断しました\n"
                f"原因: YouTube Data API の 24h クオータを使い切りました\n"
                f"対応: ~24h 後に自動再投入されるか、手動で再開: "
                f"python3 app_pipeline.py {args.vol} --from {s}"
            )
            print(f"\n{'='*60}")
            print(f"  📊 {STEP_LABELS[s]} で中断しました（YouTube quota 枯渇）")
            print(f"  再開: python3 app_pipeline.py {args.vol} --from {s}")
            print(f"{'='*60}")
            sys.exit(EXIT_QUOTA_EXHAUSTED)
        # 成功は True のみ。"retryable" / False / その他文字列はすべて最終失敗として扱う。
        succeeded = (ok is True)
        print(f"◀ STEP {s} 結果: {'OK' if succeeded else f'FAIL ({ok!r})'}")
        sys.stdout.flush()
        if not succeeded:
            ch_name = _load_dashboard_config().get("channel_name", "(unknown channel)")
            reason = "retry 上限に到達" if ok == "retryable" else "失敗"
            # QA 等、その工程を再実行しても直らない工程は前段へ差し戻す（_RESUME_OVERRIDE）。
            resume_stage = _RESUME_OVERRIDE.get(s, s)
            rollback_note = f"（{STEP_LABELS[s]} 不良 → {resume_stage} から再実行）" if resume_stage != s else ""
            _notify_line(
                f"❌ [{ch_name}] vol.{args.vol} の {STEP_LABELS[s]} で{reason}しました{rollback_note}\n"
                f"再開: python3 app_pipeline.py {args.vol} --from {resume_stage}"
            )
            print(f"\n{'='*60}")
            print(f"  ⛔ {STEP_LABELS[s]} で停止しました ({reason}){rollback_note}")
            print(f"  再開: python3 app_pipeline.py {args.vol} --from {resume_stage}")
            print(f"{'='*60}")
            sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  🎉 vol.{args.vol} の全工程が完了しました！")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
