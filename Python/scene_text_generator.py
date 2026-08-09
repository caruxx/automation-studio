"""シーンテキスト自動生成 (LLM vision 経由)

Photoshop の「都市名_テキスト」層等に流し込む英語フレーズを、AI 画像から自動生成する。

⚠ 文字のトーン・例・禁止語・構文は **チャンネル別設定（scene_text_*）から渡す**設計。
   旧仕様でハードコードしていた特定チャンネル（Harbor Notes）由来のルールは撤去済み。
   引数が空のときは persona 準拠の中立指示で生成する（特定チャンネル色を出さない）。
"""

from __future__ import annotations
import os
import re
from pathlib import Path
from typing import Optional

# 旧 Harbor Notes 固定禁止語は撤去。禁止語はチャンネル設定 scene_text_forbidden から渡す。
DEFAULT_FORBIDDEN_PHRASES: list[str] = []


def generate_scene_text_for_image(
    image_path: str,
    persona: str = "",
    *,
    tone: str = "",
    examples: Optional[list[str]] = None,
    forbidden_phrases: Optional[list[str]] = None,
    structure: str = "",
    case: str = "upper",
    punctuation: str = "none",
    claude_cli: str = "claude",
    timeout: float = 120.0,
) -> str:
    """AI生成画像を LLM vision で分析し、シーンテキスト 1 フレーズを返す。

    Args:
        image_path: 分析対象画像（vol{N}.png 等）の絶対パス
        persona: チャンネルのコンセプト（任意、プロンプトに反映）
        tone: 文字のトーン指定（チャンネル設定。空なら persona 準拠の中立）
        examples: 語感の参考フレーズ（完全コピー禁止。空なら例示しない）
        forbidden_phrases: 完全一致禁止フレーズ（空なら制約なし）
        structure: 構文ヒント（空なら汎用既定）
        case: 大文字・小文字の形式（upper / sentence）
        punctuation: 句読点の形式（none / period）
        claude_cli: LLM CLI のパス or 名前
        timeout: subprocess の最大待ち秒数

    Returns:
        指定された大文字・小文字と句読点形式の英語フレーズ

    Raises:
        FileNotFoundError: image_path が存在しない
        RuntimeError: LLM 実行失敗 / 不正な出力
    """
    img = Path(image_path).expanduser().resolve()
    if not img.exists():
        raise FileNotFoundError(f"画像が見つかりません: {img}")

    examples = examples or []
    forbidden = forbidden_phrases if forbidden_phrases is not None else DEFAULT_FORBIDDEN_PHRASES
    case = case if case in {"upper", "sentence"} else "upper"
    punctuation = punctuation if punctuation in {"none", "period"} else "none"

    tone_line = tone.strip() or "a mood that fits the channel concept/persona"
    structure_line = structure.strip() or "verb+noun or adjective+noun"

    examples_block = ""
    if examples:
        examples_block = "\n- Style reference (match the register, do NOT copy verbatim): " + " / ".join(examples)
    forbidden_block = ""
    if forbidden:
        forbidden_block = "\n- Forbidden exact matches (never output these): " + ", ".join(forbidden)
    persona_block = f"\nChannel concept/persona: {persona}\n" if persona else ""
    learned_block = ""
    try:
        import app_learning as _learning
        hint = _learning.learned_patterns_prompt_hint(os.environ.get("APP_CHANNEL_FOLDER") or "")
        if hint:
            learned_block = "\n- Learned winning patterns from this channel's 48h reviews (adapt, do not copy blindly): " + hint.replace("\n", " / ")
    except Exception:
        pass

    if case == "upper" and punctuation == "none":
        output_rule = "- Output: 2 words, ALL UPPERCASE, separated by a single space (3 words allowed only if natural)"
        language_rule = "- English only. No quotes, no punctuation, no emoji, no labels, no explanation."
        output_instruction = "Output ONLY the phrase (two or three uppercase words). Nothing else."
    elif case == "sentence" and punctuation == "period":
        output_rule = "- Output: ONE short English sentence of 2 to 5 words, using natural English capitalization; do NOT use ALL UPPERCASE"
        language_rule = "- End with exactly one period. Apostrophes are allowed. No quotes, no other punctuation, no emoji, no labels, no explanation."
        output_instruction = "Output ONLY the short sentence ending in one period. Nothing else."
    elif case == "sentence":
        output_rule = "- Output: ONE short English phrase of 2 to 5 words, using natural English capitalization; do NOT use ALL UPPERCASE"
        language_rule = "- English only. No quotes, no punctuation, no emoji, no labels, no explanation."
        output_instruction = "Output ONLY the phrase. Nothing else."
    else:
        output_rule = "- Output: 2 words, ALL UPPERCASE, separated by a single space (3 words allowed only if natural)"
        language_rule = "- End with exactly one period. Apostrophes are allowed. No quotes, no other punctuation, no emoji, no labels, no explanation."
        output_instruction = "Output ONLY the uppercase phrase ending in one period. Nothing else."

    prompt = f"""Read the image at: {img}

Based on the visual atmosphere of this image, generate ONE short English phrase that captures its mood for a long-form BGM YouTube thumbnail.

STRICT RULES:
{output_rule}
- Tone: {tone_line}
- Syntax: {structure_line}
{language_rule}{examples_block}{forbidden_block}{learned_block}
{persona_block}{output_instruction}"""

    # Claude→Codex フォールバック共通ランナー(Vision)に委譲（全機能のバックアップ回路）。
    # 画像は Claude へ --add-dir、Codex へ -i で渡る。
    from app_llm_runner import run_llm_vision
    raw = run_llm_vision(prompt, [str(img)], cli_cmd=claude_cli, timeout=timeout, label="scene-text").strip()
    if not raw:
        raise RuntimeError("LLM が空文字を返しました")

    # 最初の非空行から指定形式の英語フレーズを抽出
    for line in raw.split("\n"):
        if punctuation == "none":
            line = line.strip().strip('"').strip("'")
        else:
            line = line.strip()
            if len(line) >= 2 and (line[0], line[-1]) in {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}:
                line = line[1:-1].strip()
        if not line:
            continue
        if punctuation == "none":
            cleaned = re.sub(r"[^A-Za-z\s]", " ", line).strip()
        else:
            cleaned = re.sub(r"[^A-Za-z\s'’]", " ", line).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if case == "upper":
            cleaned = cleaned.upper()
        if punctuation == "period" and cleaned:
            cleaned = cleaned.rstrip(".") + "."
        if cleaned and 1 <= len(cleaned.split()) <= 5:
            return cleaned

    raise RuntimeError(f"LLM 出力からフレーズを抽出できず: {raw[:200]}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="シーンテキスト自動生成 (LLM vision)")
    parser.add_argument("image", help="画像パス")
    parser.add_argument("--persona", default="", help="チャンネル persona")
    parser.add_argument("--tone", default="", help="トーン指定")
    parser.add_argument("--case", choices=("upper", "sentence"), default="upper", help="大文字・小文字の形式")
    parser.add_argument("--punctuation", choices=("none", "period"), default="none", help="句読点の形式")
    parser.add_argument("--cli", default="claude", help="LLM CLI のパス")
    args = parser.parse_args()
    text = generate_scene_text_for_image(
        args.image, persona=args.persona, tone=args.tone,
        case=args.case, punctuation=args.punctuation, claude_cli=args.cli,
    )
    print(text)
