# app-export: Premiere Pro 自動書き出し

Premiere Pro のアクティブシーケンスを Adobe Media Encoder 経由で H.264 書き出しするスキル。

## 概要
AppleScript + JSX でPremiere Proの書き出しを自動化する。
`_[自動配置くん]premiere_long.jsx` の後に実行する想定。

## 実行スクリプト

### JSX ファイル: `app_export_ame.jsx`

```jsx
// 配置先: _claude/Script/app_export_ame.jsx
```

このスキルの実行時にClaude Codeが以下のJSXを生成・実行する:

### 処理内容
1. アクティブシーケンスを取得
2. プロジェクトフォルダパスを取得
3. 出力ファイル名: `vol_vol{num}.mp4`
4. H.264プリセットで Adobe Media Encoder にキューを追加
5. Media Encoder でレンダリング開始

### AppleScript 経由での実行
```bash
#!/bin/bash
# app_export.sh - Premiere Pro 書き出し自動化
JSX_PATH="${AUTOMATION_SCRIPT_DIR:-$(dirname "$0")/../Script}/app_export_ame.jsx"

osascript <<EOF
tell application "Adobe Premiere Pro 2025"
    activate
    delay 1
    do javascript file "$JSX_PATH"
end tell
EOF
```

## JSX スクリプト本体

以下をClaude Codeが `app_export_ame.jsx` として生成する:

```jsx
#target premierepro

(function () {
    try { app.enableQE(); } catch (e) { }

    function getProjectFolderPath() {
        try {
            var p = app.project.path ? String(app.project.path) : "";
            if (!p) return "";
            var projFile = new File(p);
            return projFile.parent.fsName.replace(/\\/g, "/");
        } catch (_) { return ""; }
    }

    function extractDirNumber(dirPath) {
        try {
            var name = (new Folder(dirPath)).name;
            var m = String(name).match(/^(\d+)_/);
            return m ? m[1] : "00";
        } catch (_) { return "00"; }
    }

    var seq = app.project.activeSequence;
    if (!seq) { alert("アクティブなシーケンスがありません"); return; }

    var workDir = getProjectFolderPath();
    if (!workDir) { alert("プロジェクトフォルダが見つかりません"); return; }

    var dirNum = extractDirNumber(workDir);
    var outputName = "vol_vol" + dirNum + ".mp4";
    var outputPath = workDir + "/" + outputName;

    // Media Encoder にキュー追加
    try {
        var projPath = new File(app.project.path);

        // QE API 経由で AME にエンキュー
        var qeSeq = qe.project.getActiveSequence();
        if (qeSeq) {
            // H.264 Match Source - High Bitrate プリセットパス
            var presetPath = "/Applications/Adobe Media Encoder 2025/Adobe Media Encoder 2025.app/Contents/MediaIO/systempresets/4028_4400/Match Source - High bitrate.epr";

            // プリセットファイル存在確認
            var presetFile = new File(presetPath);
            if (!presetFile.exists) {
                // フォールバック: ユーザーのプリセットフォルダを探索
                var userPresetDir = Folder("~/Documents/Adobe/Adobe Media Encoder/Presets");
                presetPath = ""; // 空なら AME がデフォルトを使用
            }

            app.encoder.launchEncoder();
            app.encoder.encodeSequence(
                seq,
                outputPath,
                presetPath,
                app.encoder.ENCODE_IN_TO_OUT,
                1  // removeOnCompletion
            );

            app.encoder.startBatch();
            alert("書き出しを開始しました:\n" + outputPath);
        }
    } catch (e) {
        alert("書き出しエラー: " + e);
    }
})();
```

## 注意事項
- Adobe Media Encoder がインストール済みであること
- Premiere Pro 2025 以降の `app.encoder` API を使用
- プリセットパスは環境依存 — 見つからない場合はAMEのデフォルト設定を使用
- 書き出し完了の検知は AME 側のジョブ監視が必要（現状は開始のみ自動化）
