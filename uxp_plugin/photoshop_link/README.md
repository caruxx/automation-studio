# Photoshop Link (UXP)

Photoshop 2026 (CEP 廃止後) 向けの Photoshop 連携パネル。Premiere Link の UXP 移植版。

## 主な役割

- **Script Launcher**: 任意のフォルダにある `.psjs` / `.js` を 1 クリックで実行
- **IPC**: Python から `/tmp/photoshop_link_uxp_trigger.json` 経由で UXP コードを実行 → `result.json` に応答

`require("photoshop")` API（`app`, `action`, `core`, `constants`, `batchPlay`）が runScript / pollIPC のスコープに注入されているので、ロードする `.psjs` 内で直接呼び出せる。

## インストール手順

### 1. UXP Developer Tool (UDT) を入手

Creative Cloud デスクトップ → 検索バーで **「UXP Developer Tool」** → インストール。

### 2. UDT で本プラグインを登録

1. UDT を起動
2. 右上「**Add Plugin**」→ このディレクトリの `manifest.json` を選択
3. プラグインカードが追加される

### 3. Photoshop にロード

1. Photoshop 2026 を起動（バージョン 26.0+ が必要、本実装は 27.5 で確認）
2. UDT のプラグインカードで「**•••**」→「**Load**」
3. Photoshop の **ウィンドウ → プラグイン → Photoshop Link** からパネルを開く

## 開発時のリロード

`index.js` / `index.html` を編集したら、UDT のカードで「**•••**」→「**Reload**」でホット反映。

## ファイル構成

```
photoshop_link/
├── manifest.json   # UXP プラグイン定義（manifestVersion 5）
├── index.html      # パネル UI
├── index.js        # ロジック（Script Launcher + IPC ポーリング）
└── icons/          # 任意（manifest.icons に登録すると Window メニューに出る）
```

## permissions の理由

| 権限 | 用途 |
|---|---|
| `localFileSystem: fullAccess` | 任意のフォルダから `.psjs` を読み込んで実行するため |
| `launchProcess: request` | 将来的に外部プロセス起動が必要な場合の保険 |
| `allowCodeGenerationFromStrings: true` | スクリプト本体を `new Function(code)` で評価するため必須 |
| `network.domains: localhost` | Python 側 API（http://localhost:8888）と通信するため |

## CEP 版との違い

| 項目 | CEP (旧) | UXP (本実装) |
|---|---|---|
| 対応 Photoshop | 〜 2024 | 2025+ (PS 2026 で必須) |
| ホスト言語 | ExtendScript (`.jsx`) | UXP JS (`.psjs` / `.js`) |
| eval | `$.evalFile()` | `new Function(code)()` |
| インストール先 | `~/Library/.../CEP/extensions/` 直接配置 | UDT 経由でロード |
| API | `app.project` 等の旧 PS DOM | `require("photoshop")` |

## Python 側 IPC サンプル

```python
import json, time
from pathlib import Path

TRIGGER = Path("/tmp/photoshop_link_uxp_trigger.json")
RESULT  = Path("/tmp/photoshop_link_uxp_result.json")

def run_uxp(code: str, timeout: float = 10.0):
    if RESULT.exists(): RESULT.unlink()
    TRIGGER.write_text(json.dumps({"code": code}))
    t0 = time.time()
    while time.time() - t0 < timeout:
        if RESULT.exists():
            return json.loads(RESULT.read_text())
        time.sleep(0.1)
    raise TimeoutError("photoshop_link IPC timeout")

# 例: アクティブドキュメント名を取得
print(run_uxp("return app.activeDocument ? app.activeDocument.name : null;"))
```
