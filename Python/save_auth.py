#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Flow にログインして storage_state を auth.json に保存するだけ。

使い方:
    python3 save_auth.py

手順:
    1) Chromium が立ち上がる
    2) ブラウザ上で Google にログインし、labs.google/fx/ja/tools/flow が
       ログイン済み状態で開ける事を確認する
    3) ターミナルに戻って Enter キーを押す
    4) auth.json が同階層に書き出される
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
FLOW_URL = "https://labs.google/fx/ja/tools/flow"
OUT = HERE / "auth.json"


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(locale="ja-JP")
        page = context.new_page()
        page.goto(FLOW_URL)
        print("\nブラウザで Google ログインを済ませてください。")
        print("Flow が普通に使える状態になったら、ここで Enter を押してください…")
        input()
        context.storage_state(path=str(OUT))
        print(f"saved: {OUT}")
        context.close()
        browser.close()


if __name__ == "__main__":
    main()
