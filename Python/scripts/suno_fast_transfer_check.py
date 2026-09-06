"""使い捨てブラウザで3MBの復号結果の転送とSHA-256一致を確認する。"""

import base64
import hashlib
import os
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from suno_fast_dl import FAST_DECRYPT_HOOK, fetch_result_bytes, register_transfer_binding


def main() -> int:
    original = os.urandom(3145728)
    session_id = "transfer-check"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto("about:blank")
            # about:blankは非secure contextなのでcrypto.subtleが存在しない。
            # 復号は検証対象外。誤って呼ばれた場合には明示的に失敗させる。
            page.evaluate("""() => {
                if (!crypto.subtle) {
                  Object.defineProperty(crypto, 'subtle', { value: {
                    decrypt() { throw new Error('転送検証では復号を実行しません'); }
                  }});
                }
            }
            """)
            page.evaluate(FAST_DECRYPT_HOOK)
            register_transfer_binding(page)
            page.evaluate("""({sessionId, size}) => {
                window.__sunoFastResults[sessionId] = {
                  status: 'done', bytes: new Uint8Array(size), size, url: 'about:blank'
                };
            }""", {"sessionId": session_id, "size": len(original)})
            # 仕込みも分割して、巨大なCDPメッセージを作らない。
            for offset in range(0, len(original), 1048576):
                encoded = base64.b64encode(original[offset:offset + 1048576]).decode("ascii")
                page.evaluate("""({sessionId, offset, base64}) => {
                    const binary = atob(base64);
                    const chunk = Uint8Array.from(binary, character => character.charCodeAt(0));
                    window.__sunoFastResults[sessionId].bytes.set(chunk, offset);
                }""", {"sessionId": session_id, "offset": offset, "base64": encoded})
            received = fetch_result_bytes(page, session_id)
        finally:
            browser.close()
    sha256_match = hashlib.sha256(original).digest() == hashlib.sha256(received).digest()
    print(f"size={len(original)} received={len(received)} sha256_match={sha256_match}")
    if len(received) != len(original) or not sha256_match:
        return 1
    print("TRANSFER OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"TRANSFER FAILED: {e}", file=sys.stderr)
        sys.exit(1)
