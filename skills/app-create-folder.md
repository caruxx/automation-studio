# app-create-folder: 動画フォルダ生成

新しい動画プロジェクト用のフォルダを自動生成するスキル。

## 概要
チャンネルフォルダ内の既存フォルダから最大連番を検出し、次の番号でフォルダを作成。
Premiere Pro テンプレートと PSD テンプレートをコピーし、シーケンス名を自動置換する。

## 実行方法

### 方法1: .command ファイルをダブルクリック
```
チャンネルフォルダ/_create_vol_dir.command
```

### 方法2: Claude Code から実行
```bash
CHANNEL_DIR="${AUTOMATION_CHANNEL_DIR:-${HOME}/path/to/YT/channel}"
cd "$CHANNEL_DIR" && bash "_create_vol_dir.command"
```

## 処理フロー
1. `{num}_{prefix}*` パターンのフォルダを走査 → 最大連番を取得
2. 公開日を対話入力（例: `2026-03-20`）
3. `{次番号}_{prefix}_{公開日}` フォルダを作成
4. テンプレートをコピー:
   - `プロジェクト/260207_vol_base.prproj` → `vol_vol{num:02d}.prproj`
   - `プロジェクト/vol_base.psd` → `vol_vol{num:02d}.psd`
5. .prproj内の「BGMシーケンス」をプロジェクト名に perl で置換（gzip展開→置換→再圧縮）

## 注意事項
- テンプレートパスは `$HOME` ベース（ユーザー名変更に対応）
- mini版 (`_【mini】create_vol_dir.command`) は同一処理 + 自動ターミナル終了
- `$SCRIPT_DIR` ベースのパスで mini版はポータブル
