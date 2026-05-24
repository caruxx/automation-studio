# Premiere Link

Adobe Premiere Pro 向けの CEP パネル。2 つの機能を持つ:

1. **JSX Launcher** — 任意のフォルダから .jsx スクリプトを読み込み、ワンクリックで実行
2. **Python IPC Bridge** — Python スクリプトからファイルポーリング経由で ExtendScript を実行（orzz. 自動化パイプライン用）

## インストール

```bash
cd cep_extension
bash install.sh
```

スクリプトの動作:
- `premiere_link/` を `~/Library/Application Support/Adobe/CEP/extensions/net.premiere.link/` にコピー
- CEP デバッグモード（未署名拡張の許可）を有効化
- 旧パネル（`pymiere_link` / `net.orzz.bridge` / `net.premier.link`）があれば退避（`.bak` 付加）

Premiere Pro を再起動すると、**ウィンドウ → 拡張機能 → Premiere Link** から開けます。
`manifest.xml` の `AutoVisible: true` / `StartOnLaunch: true` により、前回のドック位置に自動で開きます。

## アンインストール

```bash
bash install.sh uninstall
```

## 使い方

### JSX Launcher として使う

1. パネル右上の「フォルダ選択」をクリック
2. `.jsx` ファイルが入ったフォルダを指定
3. フォルダ内のスクリプトがボタン一覧で表示される
4. ボタンをクリックで実行

選択したフォルダは `localStorage` に保存され、次回起動時に自動で復元されます。

### Python IPC Bridge として使う

パネルが開いている間、200ms 間隔で `/tmp/pymiere_trigger.json` を監視しています。
Python 側から以下のファイルを書き込むと、中の `code` が ExtendScript として評価されます:

```python
import json, time
with open("/tmp/pymiere_trigger.json", "w") as f:
    json.dump({"code": 'alert("hello from Python");'}, f)
# 結果は /tmp/pymiere_result.json に書き込まれる
```

| ファイル | 用途 |
|---------|------|
| `/tmp/pymiere_trigger.json` | Python → Panel: 実行する ExtendScript コード |
| `/tmp/pymiere_result.json` | Panel → Python: 実行結果 |
| `/tmp/pymiere_ping.txt` | Panel → Python: 生存確認（5秒ごとに更新） |
| `/tmp/pymiere_activity.json` | Panel → ダッシュボード: 直近 30 件のアクティビティログ |

> IPC ファイル名は従来の `pymiere_*` 名を互換目的で維持しています。

## トラブルシューティング

| 症状 | 対処 |
|------|------|
| 拡張機能メニューに表示されない | Premiere Pro を完全終了 → 再起動。`install.sh` が PlayerDebugMode を設定済みか確認 |
| パネルが白紙 | `js/CSInterface.js` が配置されているか確認。無ければ Premiere 本体からコピー |
| ボタンが表示されない | フォルダに `.jsx` が直接入っているか確認（サブフォルダは対象外） |
| Python IPC が動かない | パネルのステータスドットが緑か確認。ping ファイル `/tmp/pymiere_ping.txt` の更新時刻をチェック |

## ファイル構成

```
cep_extension/
├── install.sh          # インストーラ
├── README.md           # このファイル
└── premiere_link/
    ├── CSXS/
    │   └── manifest.xml    # 拡張機能マニフェスト
    ├── index.html          # パネル UI
    ├── js/
    │   ├── CSInterface.js  # Adobe 公式ライブラリ
    │   └── main.js         # パネル ロジック
    └── jsx/
        └── host.jsx        # ExtendScript ヘルパ
```

## 要件

- macOS: Adobe Premiere Pro 2020 以降
- Windows: 同上（`install.sh` は Git Bash / MSYS で動作、レジストリ設定は手動）
