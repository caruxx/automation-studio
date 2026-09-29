"""公開用の曲名では、末尾のテイク番号だけを非表示にする。"""
import re


def public_track_title(title: str) -> str:
    original = str(title or "").strip()
    # (1) / （２）だけを対象にし、(Live) 等の曲名情報は保持する。
    clean = re.sub(r"(?:\s*[（(]\s*[0-9０-９]+\s*[）)])+$", "", original).strip()
    return clean or original


def public_timecode_line(line: str) -> str:
    match = re.match(r"^(\s*\d{1,2}:\d{2}(?::\d{2})?\s+-\s+)(.*)$", line)
    return match[1] + public_track_title(match[2]) if match else line
