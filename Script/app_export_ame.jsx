// Premiere Pro JSX - Media Encoder 自動書き出し
// orzz. チャンネル用: アクティブシーケンスを H.264 で書き出し
#target premierepro

(function () {
    try { app.enableQE(); } catch (e) { }

    function log(s) { try { $.writeln(s); } catch (_) { } }

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
    if (!seq) {
        alert("アクティブなシーケンスがありません。\nシーケンスを選択してから実行してください。");
        return;
    }

    var workDir = getProjectFolderPath();
    if (!workDir) {
        alert("プロジェクトフォルダが見つかりません。\n先にプロジェクトを保存してから実行してください。");
        return;
    }

    var dirNum = extractDirNumber(workDir);
    var outputName = "orzz_vol" + dirNum + ".mp4";
    var outputPath = workDir + "/" + outputName;

    // 出力先の確認
    var outputFile = new File(outputPath);
    if (outputFile.exists) {
        var overwrite = confirm("ファイルが既に存在します:\n" + outputName + "\n\n上書きしますか？");
        if (!overwrite) {
            alert("キャンセルしました");
            return;
        }
    }

    try {
        // Adobe Media Encoder を起動
        app.encoder.launchEncoder();
        $.sleep(2000); // AME 起動待ち

        // エンキュー
        // encodeSequence(sequence, outputFilePath, presetPath, workAreaType, removeOnCompletion)
        // workAreaType: 0 = ENCODE_ENTIRE, 1 = ENCODE_IN_TO_OUT, 2 = ENCODE_WORK_AREA
        var encodeSuccess = app.encoder.encodeSequence(
            seq,
            outputPath,
            "", // 空文字 = AME のデフォルトH.264プリセットを使用
            0,  // ENCODE_ENTIRE
            1   // removeOnCompletion = true
        );

        if (encodeSuccess) {
            app.encoder.startBatch();
            alert("Media Encoder に書き出しジョブを追加しました。\n\n出力先: " + outputPath +
                  "\n\nMedia Encoder で書き出しが完了するまでお待ちください。");
        } else {
            alert("エンキューに失敗しました。\nMedia Encoder が正しく起動しているか確認してください。");
        }
    } catch (e) {
        alert("書き出しエラー: " + e.message);
        log("Export error: " + e);
    }
})();
