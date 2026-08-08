"""
パスワード強度ポリシー (Tier 2 セキュリティ)

要件 (default):
- 12文字以上 / 128文字以下
- 以下4カテゴリをすべて満たす:
  - 英大文字 (A-Z)
  - 英小文字 (a-z)
  - 数字 (0-9)
  - 記号 (!@#$%^&* 等)
- 既知の弱いパターン (連続文字 / よく使われるパスワード) を拒否
"""
from __future__ import annotations

import re
from typing import Optional

MIN_LENGTH = 12
MAX_LENGTH = 128
MIN_CATEGORIES = 4

COMMON_BLACKLIST = {
    "password", "password1", "password123", "passw0rd",
    "qwerty", "qwerty123", "letmein", "welcome", "welcome1",
    "admin", "admin123", "administrator",
    "iloveyou", "abc123", "monkey", "dragon", "12345678",
    "11111111", "00000000", "asdfghjk", "zxcvbnm1",
}


def _categories(password: str) -> int:
    cats = 0
    if re.search(r"[A-Z]", password):
        cats += 1
    if re.search(r"[a-z]", password):
        cats += 1
    if re.search(r"\d", password):
        cats += 1
    if re.search(r"[^A-Za-z0-9]", password):
        cats += 1
    return cats


def _has_long_run(password: str) -> bool:
    """4文字以上の同じ文字 / 4文字以上の連続シーケンスを検出。"""
    if re.search(r"(.)\1{3,}", password):
        return True
    for i in range(len(password) - 3):
        chunk = password[i:i + 4]
        if chunk.isalnum():
            codes = [ord(c.lower()) for c in chunk]
            if all(codes[j + 1] - codes[j] == 1 for j in range(3)):
                return True
            if all(codes[j + 1] - codes[j] == -1 for j in range(3)):
                return True
    return False


def validate_password(
    password: str,
    *,
    email: Optional[str] = None,
    name: Optional[str] = None,
) -> Optional[str]:
    """弱いパスワードならエラーメッセージ (str) を返す。OK なら None。"""
    if not password:
        return "パスワードを入力してください"
    if len(password) < MIN_LENGTH:
        return f"パスワードは{MIN_LENGTH}文字以上にしてください"
    if len(password) > MAX_LENGTH:
        return f"パスワードは{MAX_LENGTH}文字以内にしてください"
    if _categories(password) < MIN_CATEGORIES:
        return "パスワードには英大文字・英小文字・数字・記号をそれぞれ1文字以上含めてください"
    lowered = password.lower()
    if lowered in COMMON_BLACKLIST:
        return "推測されやすいパスワードです。別のものを使用してください"
    if email:
        local = email.split("@", 1)[0].lower()
        if len(local) >= 3 and local in lowered:
            return "メールアドレスをパスワードに含めないでください"
    if name and len(name) >= 3 and name.lower() in lowered:
        return "ユーザー名をパスワードに含めないでください"
    if _has_long_run(password):
        return "連続する文字や同じ文字の繰り返しが多すぎます"
    return None


def assert_password_or_400(
    password: str,
    *,
    email: Optional[str] = None,
    name: Optional[str] = None,
) -> None:
    """validate_password で NG なら HTTPException 400 を投げる。"""
    err = validate_password(password, email=email, name=name)
    if err:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err)
