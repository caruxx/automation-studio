// Premiere Pro JSX - A1トラックのタイムスタンプ出力 & 字幕配置
// 開いているプロジェクトのA1トラックにあるmp3ファイルの開始時刻と名前を
// "music_time_code_info_XX.txt" として書き出します（XXはプロジェクトフォルダの接頭辞）。
// さらにSRTファイルを作成し、プロジェクトにインポートしてC1トラックに配置します。

#target premierepro

(function () {
    // ===== ユーティリティ =====
    function ensureForwardSlash(p) { return String(p).replace(/\\/g, "/"); }

    function assertPremiere() {
        if (!(app && app.project && app.project.rootItem)) {
            alert("Premiere Pro 上で実行してください（#target premierepro）");
            throw new Error("Not Premiere");
        }
    }

    function getProjectFolderPath() {
        try {
            var p = app.project && app.project.path ? String(app.project.path) : "";
            if (!p) return "";
            var projFile = new File(p);
            if (!projFile || !projFile.exists) return "";
            return ensureForwardSlash(projFile.parent.fsName);
        } catch (_) {
            return "";
        }
    }

    function resolveWorkDir() {
        var projectFolder = getProjectFolderPath();
        if (!projectFolder) {
            alert("プロジェクトファイル(.prproj)の保存場所を取得できませんでした。\n先にプロジェクトを保存してから実行してください。");
            throw new Error("project folder not found");
        }
        return projectFolder;
    }

    function extractDirNumber(dirPath) {
        try {
            var name = (new Folder(dirPath)).name;
            var m = String(name).match(/^(\d+)_/);
            return m ? m[1] : "00";
        } catch (_) {
            return "00";
        }
    }

    function getTitleFromFileName(name) {
        var noExt = String(name).replace(/\.[^\.]+$/, "");
        return noExt.replace(/^z+_/, "");
    }

    function formatTime(sec) {
        sec = Math.round(sec);
        var h = Math.floor(sec / 3600);
        var m = Math.floor((sec % 3600) / 60);
        var s = sec % 60;
        
        var mStr = (m < 10 ? "0" : "") + m;
        var sStr = (s < 10 ? "0" : "") + s;
        
        if (h > 0) {
            var hStr = (h < 10 ? "0" : "") + h;
            return hStr + ":" + mStr + ":" + sStr;
        } else {
            return mStr + ":" + sStr;
        }
    }

    function extractTrackData(seq) {
        if (!seq) {
            alert("アクティブなシーケンスがありません。");
            return null;
        }
        var aTracks = seq.audioTracks;
        if (!aTracks || aTracks.numTracks === 0) {
            alert("オーディオトラックがありません。");
            return null;
        }
        
        var track = aTracks[0]; // A1トラック
        if (!track || !track.clips || track.clips.numItems === 0) {
            alert("A1トラックにクリップがありません。");
            return null;
        }

        var clipList = [];
        for (var i = 0; i < track.clips.numItems; i++) {
            clipList.push(track.clips[i]);
        }
        
        clipList.sort(function (a, b) {
            return a.start.seconds - b.start.seconds;
        });

        var times = [];
        var subs = [];

        for (var i = 0; i < clipList.length; i++) {
            var c = clipList[i];
            var tStart = c.start.seconds;
            var tEnd = c.end.seconds;
            var name = c.projectItem ? c.projectItem.name : c.name;
            var title = getTitleFromFileName(name);

            times.push(formatTime(tStart) + " - " + title);
            
            subs.push({
                start: tStart,
                end: tEnd,
                text: title
            });
        }

        return { times: times, subs: subs };
    }

    function saveTimestampFile(arr, workDir) {
        var dirNum = extractDirNumber(workDir);
        var filePath = workDir + "/timestamp_info_" + dirNum + ".txt";

        var f = new File(filePath);
        try {
            f.lineFeed = "Windows";
            if (f.open("w")) {
                for (var i = 0; i < arr.length; i++) {
                    f.writeln(arr[i]);
                }
                f.close();
                return f.fsName;
            } else {
                alert("ファイルを開けませんでした: " + filePath);
            }
        } catch (e) {
            alert("保存中にエラー: " + e);
        }
        return null;
    }

    // === 字幕関係のユーティリティ ===
    function _captionTrackCount(seq) {
        if (!seq || !seq.captionTracks) return 0;
        if (typeof seq.captionTracks.numTracks === "number") return seq.captionTracks.numTracks;
        if (typeof seq.captionTracks.numItems === "number") return seq.captionTracks.numItems;
        return 0;
    }

    function placeSubtitleOnTimeline(srtItem, seq) {
        if (!srtItem || !seq) return false;

        try {
            // キャプショントラックをSRTから作成
            if (seq.createCaptionTrack) {
                try {
                    var created = seq.createCaptionTrack(srtItem, 0, 2);
                    if (created) return true;
                } catch (e0) { }
                try {
                    var createdLegacy = seq.createCaptionTrack(srtItem, 0);
                    if (createdLegacy) return true;
                } catch (e1) { }
            }

            var targetTrack = null;
            var capCount = _captionTrackCount(seq);
            if (capCount > 0) {
                targetTrack = seq.captionTracks[0];
            }

            // 見つからなければQE APIでトラック作成を試みる
            if (!targetTrack) {
                try {
                    app.enableQE();
                    var qeSeq = qe.project.getActiveSequence();
                    if (qeSeq && qeSeq.addCaptionTrack) {
                        qeSeq.addCaptionTrack();
                        $.sleep(300);
                        capCount = _captionTrackCount(seq);
                        if (capCount > 0) targetTrack = seq.captionTracks[capCount - 1];
                    }
                } catch (e2) { }
            }

            if (!targetTrack && _captionTrackCount(seq) > 0) {
                targetTrack = seq.captionTracks[0];
            }

            if (!targetTrack) {
                alert("キャプショントラックを自動作成できませんでした。\n手動で「新しいキャプショントラックを作成」を行ってください。");
                return false;
            }

            if (targetTrack.isLocked()) {
                alert("キャプショントラックがロックされています。解除してください。");
                return false;
            }

            $.sleep(500);

            var startTime = new Time();
            startTime.seconds = 0;
            targetTrack.overwriteClip(srtItem, startTime);

            return true;
        } catch (e) {
            alert("字幕配置エラー: " + e);
            return false;
        }
    }

    function exportSrtAndImport(subs, workDir, seq) {
        if (!subs || !subs.length) return false;

        var dirNum = extractDirNumber(workDir);
        var srtName = "timestamp_subtitles_" + dirNum + ".srt";
        var srtPath = workDir + "/" + srtName;

        function toSrtTime(rawSec) {
            var totalMs = Math.round(rawSec * 1000);
            var h = Math.floor(totalMs / 3600000);
            totalMs %= 3600000;
            var m = Math.floor(totalMs / 60000);
            totalMs %= 60000;
            var s = Math.floor(totalMs / 1000);
            var ms = totalMs % 1000;

            function pad(n, len) {
                var str = String(n);
                while (str.length < len) str = "0" + str;
                return str;
            }
            return pad(h, 2) + ":" + pad(m, 2) + ":" + pad(s, 2) + "," + pad(ms, 3);
        }

        var f = new File(srtPath);
        try {
            f.encoding = "UTF-8";
            f.lineFeed = "Windows";

            if (f.open("w")) {
                f.write("\uFEFF"); // BOM

                for (var i = 0; i < subs.length; i++) {
                    var item = subs[i];
                    if (item.end - item.start < 0.001) continue;

                    var startS = toSrtTime(item.start);
                    var endS = toSrtTime(item.end);

                    f.write((i + 1) + "\r\n");
                    f.write(startS + " --> " + endS + "\r\n");
                    f.write(item.text + "\r\n");
                    f.write("\r\n");
                }
                f.close();
            } else {
                alert("SRTファイルを開けませんでした: " + srtPath);
                return false;
            }
        } catch (e) {
            alert("SRT書き出しエラー: " + e);
            return false;
        }

        try {
            // ①フォルダー確認（ファイル書き出しができているか）
            if (!f.exists) {
                alert("SRTファイルが見つかりません: " + f.fsName);
                return false;
            }

            // ②プロジェクトにインポート
            var root = app.project.rootItem;
            var ok = app.project.importFiles([f.fsName], 1, root, 0);
            if (!ok) {
                alert("SRTの読み込みに失敗しました。");
                return false;
            }

            // インポート処理後の同期待ち
            $.sleep(500);

            // プロジェクト全体を検索するのではなく、インポート直後の最新アイテムを取得
            var srtItem = root.children[root.children.numItems - 1];

            if (srtItem && srtItem.type === ProjectItemType.CLIP) {
                // ③配置
                var placed = placeSubtitleOnTimeline(srtItem, seq);
                if (!placed) {
                    alert("SRTはインポートされましたが、タイムライン配置に失敗しました。");
                    return false;
                }
                return true;
            } else {
                alert("インポートされたSRTアイテムを取得できませんでした。");
                return false;
            }
        } catch (e) {
            alert("SRTインポート/配置処理で例外: " + e);
            return false;
        }
    }

    // ====== メイン関数 ======
    function run() {
        assertPremiere();
        
        var workDir = resolveWorkDir();
        var seq = app.project.activeSequence;
        
        var data = extractTrackData(seq);
        if (!data) return;

        var txtSavedPath = null;
        if (data.times.length > 0) {
            txtSavedPath = saveTimestampFile(data.times, workDir);
        }

        var srtSuccess = false;
        if (data.subs.length > 0) {
            srtSuccess = exportSrtAndImport(data.subs, workDir, seq);
        }

        var msg = "処理が完了しました✨\n";
        if (txtSavedPath) msg += "\n・タイムスタンプ保存: OK";
        if (srtSuccess) msg += "\n・字幕の生成＆配置: OK (C1トラック)";
        
        alert(msg);
    }

    try {
        run();
    } catch (e) {
        alert("スクリプトでエラーが発生：\n" + (e && e.message ? e.message : e));
    }
})();
