// Premiere Pro JSX - 画像のみ後から配置
// - 既に楽曲/字幕が配置済みのタイムラインを前提
// - V1 トラックの既存画像クリップ（あれば）を削除
// - selected_images.json または vol{N} 画像を読み込んで 0-5s/5-30s/30s-End に配置

#target premierepro
    (function () {
        try { app.enableQE(); } catch (e) { }

        function log(s) { try { $.writeln(s); } catch (_) { } }
        function ensureForwardSlash(p) { return String(p).replace(/\\/g, "/"); }

        function assertPremiere() {
            if (!(app && app.project && app.project.rootItem)) {
                alert("Premiere Pro 上で実行してね（#target premierepro）");
                throw new Error("Not Premiere");
            }
        }

        function extractDirNumber(dirPath) {
            var name = dirPath.replace(/.*\//, "").replace(/.*\\/, "");
            var m = name.match(/^(\d+)_/);
            return m ? m[1] : "00";
        }

        function findImageFile(workDir, baseNum, suffix) {
            var nameBase = "vol" + baseNum + (suffix || "");
            var png = new File(workDir + "/" + nameBase + ".png");
            if (png.exists) return png;
            var jpg = new File(workDir + "/" + nameBase + ".jpg");
            if (jpg.exists) return jpg;
            return null;
        }

        function fileInWorkDirIfExists(workDir, fn) {
            if (!fn) return null;
            var f = new File(workDir + "/" + fn);
            return f.exists ? f : null;
        }

        function readTextFile(f) {
            try {
                f.encoding = "UTF-8";
                if (!f.open("r")) return null;
                var s = f.read();
                f.close();
                return s;
            } catch (e) { return null; }
        }

        function parseSelectedImagesJson(txt) {
            if (!txt) return null;
            var out = { main: "", sub: [] };
            var mM = txt.match(/"main"\s*:\s*"([^"]*)"/);
            if (mM) out.main = mM[1];
            var mS = txt.match(/"sub"\s*:\s*\[([\s\S]*?)\]/);
            if (mS) {
                var arr = mS[1];
                var re = /"([^"]+)"/g, m;
                while ((m = re.exec(arr)) !== null) { out.sub.push(m[1]); }
            }
            return out;
        }

        function findOrImportBin(name) {
            var root = app.project.rootItem;
            for (var i = 0; i < root.children.numItems; i++) {
                var c = root.children[i];
                if (c.name === name && c.type === 2) return c; // BIN
            }
            return root.createBin(name);
        }

        function importIfNeeded(file, bin) {
            var fsName = ensureForwardSlash(file.fsName);
            // 既存のプロジェクトアイテムを検索
            function scanBin(b) {
                for (var i = 0; i < b.children.numItems; i++) {
                    var it = b.children[i];
                    if (it.type === 2) {
                        var r = scanBin(it);
                        if (r) return r;
                    } else {
                        try {
                            var p = it.getMediaPath();
                            if (p && ensureForwardSlash(p) === fsName) return it;
                        } catch (e) { }
                    }
                }
                return null;
            }
            var existing = scanBin(app.project.rootItem);
            if (existing) return existing;
            var ok = app.project.importFiles([fsName], true, bin, false);
            if (!ok) return null;
            return scanBin(bin);
        }

        function placeSeg(vTrack, item, sTime, eTime) {
            if (!item) return;
            if (eTime <= sTime) return;
            vTrack.overwriteClip(item, sTime);
            var pClip = null;
            for (var k = 0; k < vTrack.clips.numItems; k++) {
                var c = vTrack.clips[k];
                if (Math.abs(c.start.seconds - sTime) < 0.1) { pClip = c; break; }
            }
            if (pClip) {
                var tEnd = new Time();
                tEnd.seconds = eTime;
                pClip.end = tEnd;
            }
        }

        function clearV1(vTrack) {
            // V1 の全クリップを削除（配置し直し用）
            for (var k = vTrack.clips.numItems - 1; k >= 0; k--) {
                try { vTrack.clips[k].remove(false, false); } catch (e) { log("clip remove err: " + e); }
            }
        }

        assertPremiere();

        var seq = app.project.activeSequence;
        if (!seq) { alert("アクティブなシーケンスがありません"); return; }

        var projPath = app.project.path || "";
        if (!projPath) { alert("プロジェクトが保存されていません"); return; }

        var workDir = ensureForwardSlash(new File(projPath).parent.fsName);
        var dirNum = extractDirNumber(workDir);

        // 総尺（オーディオトラック末尾から判定）
        var totalDuration = 0;
        try {
            for (var ai = 0; ai < seq.audioTracks.numTracks; ai++) {
                var at = seq.audioTracks[ai];
                for (var ci = 0; ci < at.clips.numItems; ci++) {
                    var cc = at.clips[ci];
                    if (cc.end.seconds > totalDuration) totalDuration = cc.end.seconds;
                }
            }
        } catch (e) { }
        if (totalDuration <= 0) { alert("音声クリップが見つかりません。先に自動配置を実行してください"); return; }

        log("workDir=" + workDir + ", dirNum=" + dirNum + ", totalDuration=" + totalDuration);

        // 画像検索: selected_images.json 優先 → フォールバック vol{N}
        var imgMain = null, imgSubs = [];
        var selFile = new File(workDir + "/selected_images.json");
        if (selFile.exists) {
            var sel = parseSelectedImagesJson(readTextFile(selFile));
            if (sel) {
                var mf = fileInWorkDirIfExists(workDir, sel.main);
                if (mf) imgMain = mf;
                for (var si = 0; si < sel.sub.length; si++) {
                    var sf = fileInWorkDirIfExists(workDir, sel.sub[si]);
                    if (sf) imgSubs.push(sf);
                }
            }
        }
        if (!imgMain && imgSubs.length === 0) {
            imgMain = findImageFile(workDir, dirNum, "");
            var imgSub1 = findImageFile(workDir, dirNum, "-1");
            if (imgSub1) imgSubs.push(imgSub1);
        }
        if (!imgMain && imgSubs.length === 0) {
            alert("画像ファイルが見つかりませんでした\n(vol" + dirNum + " または selected_images.json)");
            return;
        }

        // V1 トラックの既存画像を削除
        var vTrack = seq.videoTracks.numTracks > 0 ? seq.videoTracks[0] : null;
        if (!vTrack) { alert("V1 トラックがありません"); return; }
        clearV1(vTrack);

        // セッションBIN（新規作成 or 既存）
        var sessionBinName = "01_今回だけの素材";
        var sessionBin = findOrImportBin(sessionBinName);

        var itemMain = imgMain ? importIfNeeded(imgMain, sessionBin) : null;
        var itemSubs = [];
        for (var ii = 0; ii < imgSubs.length; ii++) {
            var imp = importIfNeeded(imgSubs[ii], sessionBin);
            if (imp) itemSubs.push(imp);
        }
        var itemSub0 = itemSubs.length > 0 ? itemSubs[0] : null;

        var pt1 = 5.0, pt2 = 30.0;

        // 1. 0-5s: main
        if (totalDuration > 0) {
            var end1 = (totalDuration < pt1) ? totalDuration : pt1;
            placeSeg(vTrack, itemMain, 0, end1);
        }
        // 2. 5-30s: sub[0] || main
        if (totalDuration > pt1) {
            var end2 = (totalDuration < pt2) ? totalDuration : pt2;
            placeSeg(vTrack, itemSub0 || itemMain, pt1, end2);
        }
        // 3. 30-End: subs を N 等分、なければ sub[0] || main
        if (totalDuration > pt2) {
            var tail = itemSubs.length > 0 ? itemSubs : (itemMain ? [itemMain] : []);
            if (tail.length <= 1) {
                placeSeg(vTrack, tail[0] || null, pt2, totalDuration);
            } else {
                var rem = totalDuration - pt2;
                var per = rem / tail.length;
                for (var qi = 0; qi < tail.length; qi++) {
                    var sStart = pt2 + per * qi;
                    var sEnd = (qi === tail.length - 1) ? totalDuration : (pt2 + per * (qi + 1));
                    placeSeg(vTrack, tail[qi], sStart, sEnd);
                }
            }
        }

        log("画像のみ配置完了: main=" + (itemMain ? "1" : "0") + ", subs=" + itemSubs.length);
        try { app.project.save(); } catch (e) { }
    })();
