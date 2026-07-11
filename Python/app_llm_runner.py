#!/usr/bin/env python3
"""Claude CLI → Codex CLI フォールバック共通ランナー（全機能のバックアップ回路）。

目的: Claude が上限/エラーで失敗しても処理が止まらないよう、Codex CLI(`codex exec`)に
自動で引き継ぐ。テキスト生成・Vision の両方に対応し、全モジュールの LLM 呼び出しを
ここへ集約する。各呼び出し元は run_llm() / run_llm_vision() を使うだけでフォールバックが効く。

Claude:  `claude -p <prompt> [--allowedTools Read] [--add-dir <dir> ...]`
Codex :  `codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox
          [-i <image> ...] -o <final_msg_file>` （プロンプトは stdin で渡す）
         → -o の最終メッセージファイルを最優先で読む（無ければ stdout）。
         ⚠ -i は可変長のため位置引数プロンプトを画像として飲み込む → stdin 渡し必須。

⚠ Codex を実際に動かすには `codex login` 済みである必要がある（未認証なら 401）。
   未認証時は Claude 失敗 → Codex も失敗 となり、その旨を含む LLMError を送出する。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_CLAUDE = "claude"
DEFAULT_CODEX = "codex"

try:
    from _app_config import resolve_config_dir as _resolve_config_dir
    _CONFIG_DIR = _resolve_config_dir()
except Exception:
    _CONFIG_DIR = Path.home() / ".config" / "orzz"

_SUNO_CFG = _CONFIG_DIR / "suno_config.json"

# 環境変数でフォールバックを無効化できる（既定は有効）
_FALLBACK_ENABLED = (os.environ.get("APP_LLM_FALLBACK", "1").strip() != "0")


class LLMError(RuntimeError):
    """Claude・Codex の双方が失敗したときに送出。"""


def _resolve_clis(cli_cmd: str | None) -> tuple[str, str]:
    """claude_cli / codex_cli を suno_config.json から解決（引数 cli_cmd を Claude 側に優先）。"""
    claude_cli, codex_cli = DEFAULT_CLAUDE, DEFAULT_CODEX
    try:
        d = json.loads(_SUNO_CFG.read_text(encoding="utf-8"))
        claude_cli = (d.get("claude_cli") or DEFAULT_CLAUDE)
        codex_cli = (d.get("codex_cli") or DEFAULT_CODEX)
    except Exception:
        pass
    # 既定値の文字列 "claude" は「未指定」とみなし、suno_config の claude_cli
    # （フルパス設定）を潰さない。明示的に別パスが渡された時だけ優先する。
    if cli_cmd and cli_cmd != DEFAULT_CLAUDE:
        claude_cli = cli_cmd
    return claude_cli, codex_cli


def _claude_args(cli_path: str, prompt: str, add_dirs, allow_read: bool) -> list[str]:
    args = [cli_path, "-p", prompt]
    if allow_read:
        args += ["--allowedTools", "Read"]
    for d in (add_dirs or []):
        args += ["--add-dir", str(d)]
    return args


def _run_claude_cli(cli: str, prompt: str, timeout: int, add_dirs=None, allow_read=False):
    cli_path = shutil.which(cli) or cli
    proc = subprocess.run(
        _claude_args(cli_path, prompt, add_dirs, allow_read),
        capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
    )
    return proc.returncode, (proc.stdout or ""), (proc.stderr or "")


def _run_codex_cli(cli: str, prompt: str, timeout: int, image_paths=None):
    cli_path = shutil.which(cli) or cli
    fd, out_path = tempfile.mkstemp(suffix=".txt", prefix="codex_final_")
    os.close(fd)
    args = [cli_path, "exec", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", "-o", out_path]
    for img in (image_paths or []):
        args += ["-i", str(img)]
    # ⚠ -i/--image は可変長オプションのため、直後に置いた位置引数プロンプトを
    #   画像パスとして飲み込む。プロンプトは stdin 経由で渡す（codex exec は
    #   位置引数が無いとき stdin から読む仕様）。
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, input=prompt,
        )
        final = ""
        try:
            final = Path(out_path).read_text(encoding="utf-8").strip()
        except Exception:
            final = ""
        if not final:  # -o が空なら stdout から拾う
            final = (proc.stdout or "").strip()
        return proc.returncode, final, (proc.stderr or "")
    finally:
        try:
            os.unlink(out_path)
        except Exception:
            pass


def _short(s: str, n: int = 220) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s[:n]


def run_llm(prompt: str, *, cli_cmd: str | None = None, timeout: int = 300,
            add_dirs=None, allow_read: bool = False, image_paths=None,
            label: str = "") -> str:
    """LLM テキスト生成。Claude→Codex フォールバック。最終テキストを返す。

    add_dirs/allow_read/image_paths は Claude/Codex 双方の入力（ディレクトリ許可・画像）に使う。
    Vision は run_llm_vision() を推奨。
    """
    claude_cli, codex_cli = _resolve_clis(cli_cmd)
    paths = [Path(p) for p in (image_paths or [])]
    # Vision 時は画像の親ディレクトリを Claude に許可
    eff_add_dirs = list(add_dirs or [])
    if paths:
        allow_read = True
        for p in paths:
            d = str(p.parent)
            if d and d not in eff_add_dirs:
                eff_add_dirs.append(d)

    # 1) Claude を試す
    claude_reason = ""
    try:
        rc, out, err = _run_claude_cli(claude_cli, prompt, timeout,
                                       add_dirs=eff_add_dirs, allow_read=allow_read)
        if rc == 0 and out.strip():
            return out
        claude_reason = f"rc={rc}: {_short(err or out)}"
    except subprocess.TimeoutExpired:
        claude_reason = f"timeout {timeout}s"
    except FileNotFoundError:
        claude_reason = f"claude CLI 未検出: {claude_cli}"
    except Exception as e:
        claude_reason = _short(str(e))

    if not _FALLBACK_ENABLED:
        raise LLMError(f"Claude 失敗（フォールバック無効 APP_LLM_FALLBACK=0）: {claude_reason}")

    # 2) Codex にフォールバック
    tag = f" [{label}]" if label else ""
    print(f"  ⚠ Claude 失敗（{claude_reason}）→ Codex CLI にフォールバック{tag}")
    codex_reason = ""
    try:
        rc, out, err = _run_codex_cli(codex_cli, prompt, timeout, image_paths=paths)
        if rc == 0 and out.strip():
            print(f"  ✓ Codex フォールバック成功{tag}")
            return out
        codex_reason = f"rc={rc}: {_short(err or out)}"
    except subprocess.TimeoutExpired:
        codex_reason = f"timeout {timeout}s"
    except FileNotFoundError:
        codex_reason = f"codex CLI 未検出: {codex_cli}"
    except Exception as e:
        codex_reason = _short(str(e))

    hint = ""
    if any(k in codex_reason.lower() for k in ("unauthorized", "sign in", "401", "login", "refresh token")):
        hint = "（Codex 未認証の可能性 → `codex login` を実行してください）"
    raise LLMError(f"Claude も Codex も失敗。claude=[{claude_reason}] codex=[{codex_reason}]{hint}")


def run_llm_vision(prompt: str, image_paths, *, cli_cmd: str | None = None,
                   timeout: int = 300, label: str = "") -> str:
    """Vision 生成。Claude(--add-dir + --allowedTools Read) → Codex(-i 画像) フォールバック。"""
    return run_llm(prompt, cli_cmd=cli_cmd, timeout=timeout,
                   image_paths=image_paths, allow_read=True, label=label or "vision")


if __name__ == "__main__":
    # スタブで配線確認（実 CLI を呼ばない）
    import types
    g = globals()
    g["_run_claude_cli"] = lambda *a, **k: (1, "", "Claude usage limit reached")
    g["_run_codex_cli"] = lambda *a, **k: (0, '{"ok":true,"via":"codex"}', "")
    print("fallback →", run_llm("test", label="dry"))
    g["_run_codex_cli"] = lambda *a, **k: (1, "", "401 Unauthorized: please sign in again")
    try:
        run_llm("test")
    except LLMError as e:
        print("both fail →", e)
