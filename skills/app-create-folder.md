# app-create-folder: 動画フォルダ生成

新しい動画プロジェクト用のフォルダを自動生成するスキル。

## 概要
チャンネルフォルダ内の既存フォルダから最大連番を検出し、次の番号でフォルダを作成。
テンプレートのコピーは行わない（Premiere / PSD テンプレートは 2026-10-25 段階 1a で廃止）。

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
4. （旧）テンプレートコピーと .prproj のシーケンス名置換は廃止。現在は `POST /api/videos/create` がフォルダだけを作る

## 注意事項
- テンプレートパスは `$HOME` ベース（ユーザー名変更に対応）
- mini版 (`_【mini】create_vol_dir.command`) は同一処理 + 自動ターミナル終了
- `$SCRIPT_DIR` ベースのパスで mini版はポータブル
