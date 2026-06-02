"""シーンテキスト自動生成 (Claude CLI vision 経由)

Harbor Notes 等の Photoshop 「都市名_テキスト」層に流し込む英大文字フレーズを、
ベンチマーク先のサムネ文字規則性に従って AI 画像から自動生成する。

規則性 (Harbor Notes のライバル UCdeT3oQRE_JpXuhqWbIJ_hw の実例 8 件分析):
- 英語2-3語、全大文字
- セリフ系の語感 (Trajan/Cinzel 風)
- 静寂・集中・没入・リゾート・海・内省のトーン
- 構文: 動詞+名詞 (DIVE IN, FIND BALANCE) or 形容詞+名詞 (DEEP SILENCE)
- ベンチマーク実例との完全一致は禁止、似たニュアンスの別ワード
"""

from __future__ import annotations
import re
import subprocess
from pathlib import Path
from typing import Optional

# Harbor Notes のライバルチャンネルのサムネで実際に使われていた文字 (完全一致禁止リスト)
DEFAULT_FORBIDDEN_PHRASES = [
    "DEEP SILENCE", "RELAX FLOW", "NOTHING ELSE", "PEACE MODE",
    "FIND BALANCE", "DIVE IN", "WAITING FOR YOU", "SCENT OF THE SEA",
]


def generate_scene_text_for_image(
    image_path: str,
    persona: str = "",
    forbidden_phrases: Optional[list[str]] = None,
    claude_cli: str = "claude",
    timeout: float = 120.0,
) -> str:
    """AI生成画像を Claude CLI vision で分析し、シーンテキスト 1 フレーズを返す。

    Args:
        image_path: 分析対象画像（vol{N}.png 等）の絶対パス
        persona: チャンネルのコンセプト (任意、プロンプトに反映)
        forbidden_phrases: 完全一致禁止のフレーズリスト（未指定時はベンチマーク既存ワード）
        claude_cli: Claude CLI のパス or 名前
        timeout: subprocess の最大待ち秒数

    Returns:
        英大文字 2-3 語のフレーズ (例: "BLUE HORIZON")

    Raises:
        FileNotFoundError: image_path が存在しない
        RuntimeError: Claude CLI 実行失敗 / 不正な出力
    """
    img = Path(image_path).expanduser().resolve()
    if not img.exists():
        raise FileNotFoundError(f"画像が見つかりません: {img}")

    forbidden = forbidden_phrases if forbidden_phrases is not None else DEFAULT_FORBIDDEN_PHRASES
    forbidden_str = ", ".join(forbidden)

    persona_block = f"\nChannel concept reference: {persona}\n" if persona else ""

    prompt = f"""Read the image at: {img}

Based on the visual atmosphere of this image, generate ONE short English phrase that captures its mood.

STRICT RULES:
- Output: 2 words, ALL UPPERCASE, separated by a single space (occasionally 3 words allowed if needed)
- Tone: serene, focused, immersive, resort, ocean, introspective (similar to "DEEP SILENCE" / "PEACE MODE" / "RELAX FLOW")
- Syntax: verb+noun (like "DIVE IN") or adjective+noun (like "DEEP SILENCE")
- FORBIDDEN exact matches (do NOT use any of these): {forbidden_str}
- Generate something similar in spirit but with original wording
{persona_block}
Output ONLY the phrase. No quotes. No explanation. No prefix/suffix. Just the words.
Example valid output: BLUE HORIZON"""

    # Claude→Codex フォールバック共通ランナー(Vision)に委譲（全機能のバックアップ回路）。
    # 画像は Claude へ --add-dir、Codex へ -i で渡る。
    from app_llm_runner import run_llm_vision
    raw = run_llm_vision(prompt, [str(img)], cli_cmd=claude_cli, timeout=timeout, label="scene-text").strip()
    if not raw:
        raise RuntimeError("LLM が空文字を返しました")

    # 最初の非空行から英大文字 + スペースだけを抽出
    for line in raw.split("\n"):
        line = line.strip().strip('"').strip("'")
        if not line:
            continue
        # アルファベット + スペース のみ (記号等は除去)
        cleaned = re.sub(r"[^A-Za-z\s]", " ", line).strip()
        cleaned = re.sub(r"\s+", " ", cleaned).upper()
        if cleaned and 1 <= len(cleaned.split()) <= 5:
            # 禁止フレーズ完全一致回避（caller 側でも担保するがここでもチェック）
            if cleaned in [p.upper() for p in forbidden]:
                # たまたま重なった場合は、警告だけ出して返す（再生成は caller の責任）
                pass
            return cleaned

    raise RuntimeError(f"Claude CLI 出力からフレーズを抽出できず: {raw[:200]}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="シーンテキスト自動生成 (Claude CLI vision)")
    parser.add_argument("image", help="画像パス")
    parser.add_argument("--persona", default="", help="チャンネル persona")
    parser.add_argument("--cli", default="claude", help="Claude CLI のパス")
    args = parser.parse_args()
    text = generate_scene_text_for_image(args.image, persona=args.persona, claude_cli=args.cli)
    print(text)
