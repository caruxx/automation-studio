// Premiere Pro JSX - BGM並べ＆thumbnail.jpg差し替え
// - 開いている Premiere プロジェクト（.prproj）が置かれているフォルダ配下の music 内 mp3 を取り込み
// - mp3 を「現在のプロジェクト名（.prproj除去）」シーケンスに順番で並べる
// - 「01_今回だけの素材」内の thumbnail.jpg を <プロジェクトフォルダ>/thumbnail.jpg に置き換え

#target premierepro
    //#targetengine "session" // Removed to avoid potential context issues with Window constructor

    (function () {
        try { app.enableQE(); } catch (e) { }
        // var SESSION_BIN_NAME = "01_今回だけの素材"; // 動的に生成するため削除

        // ===== ユーティリティ =====
        function log(s) { try { $.writeln(s); } catch (_) { } }
        function ensureForwardSlash(p) { return String(p).replace(/\\/g, "/"); }

        function assertPremiere() {
            if (!(app && app.project && app.project.rootItem)) {
                alert("Premiere Pro 上で実行してね（#target premierepro）");
                throw new Error("Not Premiere");
            }
        }
        function getSequenceNameFromProject() {
            try {
                var rawName = (app && app.project && app.project.name) ? String(app.project.name) : "";
                var trimmed = rawName.replace(/\.prproj$/i, "");
                return trimmed ? trimmed : "BGMシーケンス";
            } catch (_) {
                return "BGMシーケンス";
            }
        }

        function folderExists(path) {
            try { var f = new Folder(path); return !!(f && f.exists); } catch (_) { return false; }
        }
        function fileExists(path) {
            try { var f = new File(path); return !!(f && f.exists); } catch (_) { return false; }
        }

        function getHomeDir() {
            try {
                var h = Folder("~");
                if (h && h.exists) return ensureForwardSlash(h.fsName);
            } catch (_) { }
            return "";
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
                alert("プロジェクトファイル(.prproj)の保存場所を取得できないよ。\n先にプロジェクトを保存してから実行してね。");
                throw new Error("project folder not found");
            }
            return projectFolder;
        }

        function listMp3s(dir) {
            var f = new Folder(dir + "/music");
            if (!f.exists) return [];
            var arr = f.getFiles(function (x) {
                return (x instanceof File) && (/\.mp3$/i).test(String(x.name));
            }) || [];

            // グループ分け: z_付き と それ以外
            var zItems = [];
            var normalItems = [];

            for (var i = 0; i < arr.length; i++) {
                var name = String(arr[i].name);
                if (/^z+_/.test(name)) {
                    zItems.push(arr[i]);
                } else {
                    normalItems.push(arr[i]);
                }
            }

            // zItems を zの数が多い順にソート
            zItems.sort(function (a, b) {
                var nameA = String(a.name);
                var nameB = String(b.name);
                var matchA = nameA.match(/^(z+)_/);
                var matchB = nameB.match(/^(z+)_/);
                var lenA = matchA ? matchA[1].length : 0;
                var lenB = matchB ? matchB[1].length : 0;
                return lenB - lenA; // 降順 (zzz > zz > z)
            });

            // normalItems をランダムシャッフル
            for (var i = normalItems.length - 1; i > 0; i--) {
                var j = Math.floor(Math.random() * (i + 1));
                var temp = normalItems[i];
                normalItems[i] = normalItems[j];
                normalItems[j] = temp;
            }

            // 結合 (zItems優先)
            return zItems.concat(normalItems);
        }

        // ...

        function getTitleFromFileName(name) {
            // 拡張子削除
            var noExt = String(name).replace(/\.[^\.]+$/, "");
            // z_ プレフィックス削除 (字幕用)
            return noExt.replace(/^z+_/, "");
        }

        function getOrCreateSessionBin(binName) {
            var root = app.project.rootItem;
            var n = root.children ? root.children.numItems : 0;
            for (var i = 0; i < n; i++) {
                var it = root.children[i];
                if (it && it.type === ProjectItemType.BIN && it.name === binName) {
                    return it;
                }
            }
            return root.createBin(binName);
        }

        function importIfNeeded(fileObj, bin) {
            // 既に同じメディアが読み込まれていたらそれを返す（パス一致）
            var want = ensureForwardSlash(fileObj.fsName);
            function findByPath(binItem) {
                try {
                    var c = binItem.children ? binItem.children.numItems : 0;
                    for (var i = 0; i < c; i++) {
                        var it = binItem.children[i];
                        if (!it) continue;
                        if (it.type === ProjectItemType.BIN) {
                            var hit = findByPath(it);
                            if (hit) return hit;
                        } else {
                            try {
                                var p = it.getMediaPath && it.getMediaPath();
                                if (p && ensureForwardSlash(p) === want) return it;
                            } catch (_) { }
                        }
                    }
                } catch (_) { }
                return null;
            }

            var existing = findByPath(app.project.rootItem);
            if (existing) return existing;

            // import
            try {
                // Google Drive ファイル同期待ち
                $.sleep(300);
                app.project.importFiles([fileObj.fsName], 1, bin, 0);
                // インポート後の同期待ち
                $.sleep(500);
                // bin の末尾が今入れたアイテム
                var last = bin.children[bin.children.numItems - 1];
                if (last && last.type === ProjectItemType.CLIP) return last;
            } catch (e) {
                log("import error: " + e);
            }
            // フォールバック探索
            return findByPath(app.project.rootItem);
        }

        function ensureSequence(name, seedItem) {
            var seqs = app.project.sequences;
            for (var i = 0; i < seqs.numSequences; i++) {
                if (seqs[i].name === name) return { seq: seqs[i], isNew: false };
            }
            if (!seedItem) {
                alert("シーケンスが無いので作成するための種クリップが必要です（mp3でもOK）");
                throw new Error("no seed clip");
            }
            app.project.createNewSequenceFromClips(name, [seedItem], app.project.rootItem);
            // createNewSequenceFromClips はそのシーケンスをアクティブにする
            return { seq: app.project.activeSequence, isNew: true };
        }

        // --- MP3 の実長さはトラック差分で測る ---
        function getAudioTrackEndSec(aTrack) {
            var end = 0;
            try {
                var cs = aTrack.clips;
                for (var i = 0; i < cs.numItems; i++) {
                    var c = cs[i];
                    var e = (c.end && c.end.seconds) || 0;
                    if (e > end) end = e;
                }
            } catch (_) { }
            return end;
        }
        function getWritableAudioTrack(seq) {
            try {
                for (var i = 0; i < seq.audioTracks.numTracks; i++) {
                    var t = seq.audioTracks[i];
                    try { if (t && !t.isLocked()) return t; } catch (_) { if (t) return t; }
                }
            } catch (_) { }
            try { return seq.audioTracks[0]; } catch (_) { return null; }
        }
        function placeAtAudio(aTrack, item, cursorSec) {
            var before = getAudioTrackEndSec(aTrack);
            aTrack.overwriteClip(item, cursorSec);
            var after = getAudioTrackEndSec(aTrack);
            var d = Math.max(0, after - before);
            // だめならフッテージ長を試す
            if (!(d > 0)) {
                try {
                    var t = item.getFootageDuration && item.getFootageDuration();
                    if (t && typeof t.seconds === "number" && t.seconds > 0) d = t.seconds;
                } catch (_) { }
            }
            if (!(d > 0) || !isFinite(d)) d = 1.0;
            return d;
        }

        // ===========================================================
        function _normalizeFsPath(p) {
            try { var f = new File(p); if (f.exists) return f.fsName; } catch (e) { }
            return p;
        }
        function _endsWithCi(a, b) {
            if (!a || !b) return false;
            a = String(a).toLowerCase(); b = String(b).toLowerCase();
            return a.indexOf(b, a.length - b.length) !== -1;
        }
        function _isFootage(it) {
            if (!it) return false;
            if (it.type === 2) return false; // BIN
            try { var mp = it.getMediaPath && it.getMediaPath(); } catch (e) { mp = null; }
            return !!mp;
        }
        function _findItemByNameInBinFootageOnly(binItem, nameLower) {
            if (!binItem || binItem.type !== 2) return null; // needs BIN
            var kids = binItem.children; if (!kids) return null;
            for (var i = 0; i < kids.numItems; i++) {
                var ch = kids[i]; if (!ch) continue;
                if (ch.type === 2) {
                    var hit = _findItemByNameInBinFootageOnly(ch, nameLower);
                    if (hit) return hit;
                } else {
                    if (_isFootage(ch) && ch.name && ch.name.toLowerCase() === nameLower) return ch;
                }
            }
            return null;
        }
        function _findItemByNameInBin(binItem, nameLower) {
            if (!binItem || binItem.type !== 2) return null; // needs BIN
            var kids = binItem.children; if (!kids) return null;
            for (var i = 0; i < kids.numItems; i++) {
                var ch = kids[i]; if (!ch) continue;
                if (ch.type === 2) {
                    var hit = _findItemByNameInBin(ch, nameLower);
                    if (hit) return hit;
                } else {
                    if (ch.name && ch.name.toLowerCase() === nameLower) return ch;
                }
            }
            return null;
        }
        function _captionTrackCount(seq) {
            if (!seq || !seq.captionTracks) return 0;
            if (typeof seq.captionTracks.numTracks === "number") return seq.captionTracks.numTracks;
            if (typeof seq.captionTracks.numItems === "number") return seq.captionTracks.numItems;
            return 0;
        }
        function _tryChangeMediaPath(targetItem, fsPath) {
            try {
                targetItem.changeMediaPath(fsPath, true); // bool 版
            } catch (e1) { }
            try {
                var now1 = targetItem.getMediaPath && targetItem.getMediaPath();
                if (_endsWithCi(now1, fsPath)) return true;
            } catch (e) { }

            try {
                targetItem.changeMediaPath(fsPath, 1); // 数値版
            } catch (e2) { }
            try {
                var now2 = targetItem.getMediaPath && targetItem.getMediaPath();
                if (_endsWithCi(now2, fsPath)) return true;
            } catch (e3) { }

            return false;
        }


        // ===========================================================
        /**
         * シーケンス内のオーディオクリップ開始時刻(タイムライン位置)を MM:SS で配列返す
         * Object.keys / Array.map 非依存版
         */
        function getBgmStartTimesMMSS(seq) {
            seq = seq || app.project.activeSequence;
            if (!seq) { alert("アクティブなシーケンスが見つからないよ🥲"); return []; }

            // 秒 → "MM:SS"
            function mmss(sec) {
                sec = Math.max(0, Math.floor(sec + 0.5)); // 四捨五入
                var m = Math.floor(sec / 60), s = sec % 60;
                return (m < 10 ? "0" : "") + m + ":" + (s < 10 ? "0" : "") + s;
            }

            // 開始秒を重複なしで集める
            var starts = {};
            var t, tr, i, clip, sec, key;

            for (t = 0; t < seq.audioTracks.numTracks; t++) {
                tr = seq.audioTracks[t];
                if (!tr || !tr.clips) continue;
                for (i = 0; i < tr.clips.numItems; i++) {
                    clip = tr.clips[i];
                    if (!clip || !clip.start) continue;

                    sec = (typeof clip.start.seconds === "number")
                        ? clip.start.seconds
                        : (clip.start.ticks ? clip.start.ticks / 254016000000 : 0); // ticks→秒

                    key = Math.round(sec * 1000) / 1000; // 端数ブレ対策
                    starts[key] = true;
                }
            }

            // 昇順にして "MM:SS" へ（Object.keys/Array.map を使わない）
            var arr = [];
            for (var k in starts) { if (starts.hasOwnProperty(k)) arr.push(parseFloat(k)); }
            arr.sort(function (a, b) { return a - b; });

            var out = [];
            for (i = 0; i < arr.length; i++) out.push(mmss(arr[i]));
            return out;
        }


        // === 目標時間をダイアログで取得 ===
        function askTargetDuration() {
            var input = prompt(
                "ループ終了時間を入力してね\n" +
                "・H:M:S 形式  例: 3:33:33\n" +
                "・秒数のみ    例: 12813\n" +
                "(キャンセルで中断)",
                "1:00:00"
            );
            if (input === null || String(input) === "") return null;

            input = String(input).replace(/\s/g, "");

            // H:M:S 形式
            var hmsMatch = input.match(/^(\d+):(\d+):(\d+)$/);
            if (hmsMatch) {
                var h = parseInt(hmsMatch[1], 10);
                var m = parseInt(hmsMatch[2], 10);
                var s = parseInt(hmsMatch[3], 10);
                var total = h * 3600 + m * 60 + s;
                if (total > 0) return total;
                alert("0より大きい時間を入力してください");
                return null;
            }

            // 秒数のみ
            var secMatch = input.match(/^\d+$/);
            if (secMatch) {
                var totalSec = parseInt(input, 10);
                if (totalSec > 0) return totalSec;
                alert("0より大きい秒数を入力してください");
                return null;
            }

            alert("形式が正しくないよ。\nH:M:S (例: 3:33:33) または 秒数 (例: 12813) で入力してね");
            return null;
        }

        // === フェードアウト適用 (最後 fadeSec 秒をキーフレームで -96dB へ) ===
        function applyAudioFadeOut(aTrack, targetSec, fadeSec) {
            var fadeStartSec = Math.max(0, targetSec - fadeSec);
            var applied = 0;

            for (var i = 0; i < aTrack.clips.numItems; i++) {
                var clip = aTrack.clips[i];
                if (!clip) continue;
                var clipEndSec   = clip.end   ? clip.end.seconds   : 0;
                var clipStartSec = clip.start ? clip.start.seconds : 0;
                if (clipEndSec <= fadeStartSec) continue; // フェード範囲外

                var components = clip.components;
                if (!components) continue;

                for (var ci = 0; ci < components.numItems; ci++) {
                    var comp = components[ci];
                    if (!comp || !comp.properties) continue;
                    var cName = String(comp.displayName || "").toLowerCase();
                    if (cName.indexOf("volume") === -1 && cName.indexOf("ボリューム") === -1) continue;

                    for (var pi = 0; pi < comp.properties.numItems; pi++) {
                        var param = comp.properties[pi];
                        if (!param) continue;
                        var pName = String(param.displayName || "").toLowerCase();
                        if (pName.indexOf("level") === -1 && pName.indexOf("レベル") === -1) continue;

                        try {
                            // フェード開始キーフレーム (0 dB = 現状維持)
                            var tFadeStart = new Time();
                            tFadeStart.seconds = Math.max(clipStartSec, fadeStartSec);
                            param.addKey(tFadeStart);
                            param.setValueAtKey(tFadeStart, 0.0);

                            // フェード終了キーフレーム (-96 dB ≈ 無音)
                            var tFadeEnd = new Time();
                            tFadeEnd.seconds = Math.min(clipEndSec, targetSec);
                            param.addKey(tFadeEnd);
                            param.setValueAtKey(tFadeEnd, -96.0);

                            applied++;
                            log("フェードアウト clip[" + i + "]: " + tFadeStart.seconds + "s -> " + tFadeEnd.seconds + "s");
                        } catch (e) {
                            log("フェードキーフレームエラー[" + i + "]: " + e);
                        }
                        break; // Level プロパティは最初の1つで十分
                    }
                    break; // Volume コンポーネントは最初の1つで十分
                }
            }
            log("フェードアウト適用クリップ数: " + applied);
        }

        /**
         * 名前でシーケンスを探す（見つからなければ null）
         */
        function findSequenceByName(name) {
            var seqs = app.project.sequences;
            for (var i = 0; i < seqs.numSequences; i++) {
                var s = seqs[i];
                if (s && s.name === name) return s;
            }
            return null;
        }
        /**
         * album_info_XX.txt として保存
         * ディレクトリ名 (例: "01_", "23_") の数字部分をそのままファイル名に使う
         */
        function saveTimesToTxtByDir(arr, workDir) {
            var folder = new Folder(workDir);
            if (!folder.exists) {
                alert("出力先ディレクトリが存在しません: " + workDir);
                return false;
            }

            // ディレクトリ名から数字部分を抽出
            var baseName = folder.name;     // 例: "01_" or "23_"
            var m = baseName.match(/^(\d+)_/);
            var num = m ? m[1] : "00";      // 見つからなければ00に

            var filePath = workDir + "/music_time_code_info_" + num + ".txt";

            var f = new File(filePath);
            try {
                // Windows向けの改行対策
                f.lineFeed = "Windows";
                if (f.open("w")) {
                    for (var i = 0; i < arr.length; i++) {
                        f.writeln(arr[i]);
                    }
                    f.close();
                    // alert("保存しました: " + f.fsName);
                    return true;
                } else {
                    alert("ファイルを開けなかったよ: " + filePath);
                }
            } catch (e) {
                alert("保存中にエラー: " + e);
            }
            return false;
        }

        // ディレクトリ名から数字だけ抜き出す
        function extractDirNumber(dirPath) {
            try {
                var name = (new Folder(dirPath)).name;   // 例: "23_"
                var m = String(name).match(/^(\d+)_/);
                return m ? m[1] : "00";
            } catch (_) {
                return "00";
            }
        }
        // ===========================================================

        // ====== メイン関数 ======

        // ===========================================================




        // === 字幕配置関数 (C1固定) ===
        function placeSubtitleOnTimeline(srtItem, seq) {
            if (!srtItem || !seq) return false;

            try {
                // 最優先: SRT からキャプショントラックを直接作成する。
                // createCaptionTrack(projectItem, startAtSeconds, captionFormat)
                // captionFormat は 2(Subtitle) を優先。
                if (seq.createCaptionTrack) {
                    try {
                        var created = seq.createCaptionTrack(srtItem, 0, 2);
                        if (created) {
                            log("createCaptionTrack で字幕を配置しました: " + srtItem.name);
                            return true;
                        }
                    } catch (e0) {
                        log("createCaptionTrack(Subtitle) 失敗: " + e0);
                    }
                    // 旧API/環境差分向けフォールバック
                    try {
                        var createdLegacy = seq.createCaptionTrack(srtItem, 0);
                        if (createdLegacy) {
                            log("createCaptionTrack(legacy) で字幕を配置しました: " + srtItem.name);
                            return true;
                        }
                    } catch (e1) {
                        log("createCaptionTrack(legacy) 失敗: " + e1);
                    }
                }

                var targetTrack = null;
                var capCount = _captionTrackCount(seq);
                if (capCount > 0) {
                    targetTrack = seq.captionTracks[0];
                }

                // 互換フォールバック: トラックを作って overwriteClip を試す
                if (!targetTrack) {
                    log("キャプショントラックの作成を試みます...");

                    try {
                        app.enableQE();
                        var qeSeq = qe.project.getActiveSequence();
                        if (qeSeq && qeSeq.addCaptionTrack) {
                            qeSeq.addCaptionTrack();
                            $.sleep(300);
                            capCount = _captionTrackCount(seq);
                            if (capCount > 0) {
                                targetTrack = seq.captionTracks[capCount - 1];
                                log("QE APIでキャプショントラックを作成しました。");
                            }
                        }
                    } catch (e2) {
                        log("QE APIでのトラック作成失敗: " + e2);
                    }
                }

                // 最終確認
                if (!targetTrack && _captionTrackCount(seq) > 0) {
                    targetTrack = seq.captionTracks[0];
                }

                if (!targetTrack) {
                    // 自動化前提: alert は出さずログにのみ出力して継続失敗
                    log("[ERROR] キャプショントラック(C1)が見つかりません。テキストパネル＞キャプション＞「新しいキャプショントラックを作成」を1度実行してください。");
                    return false;
                }

                if (targetTrack.isLocked()) {
                    log("[ERROR] キャプショントラックがロックされています。解除してください。");
                    return false;
                }

                log("キャプショントラック(C1)を使用します: " + targetTrack.name);

                // 同期を待つための微小なスリープ
                $.sleep(500);

                // タイムライン開始位置(0秒)に配置 (上書き)
                var startTime = new Time();
                startTime.seconds = 0;
                targetTrack.overwriteClip(srtItem, startTime);

                log("字幕を配置しました: " + srtItem.name);

                // === C1 トラックスタイル「文字配置」を試行（Premiere 26 新機能）===
                // ExtendScript の Caption API は限定的。setTrackStyle / applyTrackStyle / qe DOM を順に試す。
                // どれも失敗した場合はログだけ残してフローは継続する（手動適用可能）。
                try {
                    var styleName = "文字配置";
                    var applied = false;
                    // 1. 直接プロパティ
                    if (typeof targetTrack.setTrackStyle === "function") {
                        try { targetTrack.setTrackStyle(styleName); applied = true; log("[trackStyle] setTrackStyle で適用: " + styleName); } catch (e1) { log("[trackStyle] setTrackStyle 失敗: " + e1); }
                    }
                    // 2. trackStyle プロパティ
                    if (!applied && typeof targetTrack.trackStyle !== "undefined") {
                        try { targetTrack.trackStyle = styleName; applied = true; log("[trackStyle] .trackStyle 代入で適用: " + styleName); } catch (e2) { log("[trackStyle] .trackStyle 代入失敗: " + e2); }
                    }
                    // 3. QE DOM 経由
                    if (!applied) {
                        try {
                            if (qe && qe.project && qe.project.getActiveSequence) {
                                var qSeq = qe.project.getActiveSequence();
                                if (qSeq && qSeq.getCaptionTrackAt) {
                                    var qCap = qSeq.getCaptionTrackAt(0);
                                    if (qCap && typeof qCap.setStyle === "function") {
                                        qCap.setStyle(styleName); applied = true;
                                        log("[trackStyle] QE.captionTrack.setStyle で適用: " + styleName);
                                    }
                                }
                            }
                        } catch (e3) { log("[trackStyle] QE 経由失敗: " + e3); }
                    }
                    if (!applied) log("[trackStyle] 自動適用 API 未対応。Premiere 上で手動適用が必要: " + styleName);
                } catch (eStyle) {
                    log("[trackStyle] 例外: " + eStyle);
                }

                return true;
            } catch (e) {
                log("[ERROR] 字幕配置エラー: " + e);
                return false;
            }
        }

        // === オーディオハードリミット適用関数 ===
        /**
            * 全オーディオトラックに-1dBハードリミッターを適用
            * @param {Sequence} seq - 対象シーケンス
            */
        function applyHardLimiterToAllAudioTracks(seq) {
            if (!seq) return;

            try {
                var numTracks = seq.audioTracks.numTracks;
                var appliedCount = 0;

                // Hard Limiter エフェクトを事前に1回だけ検索
                var limiterMatchNames = [
                    "AudibleFX Hard Limiter",
                    "Hard Limiter",
                    "Limiter"
                ];

                var limiter = null;
                for (var m = 0; m < limiterMatchNames.length; m++) {
                    try {
                        limiter = app.findMatchName(limiterMatchNames[m]);
                        if (limiter) break;
                    } catch (e) {
                        continue;
                    }
                }

                if (!limiter) {
                    log("Hard Limiterエフェクトが見つかりません");
                    return;
                }

                for (var i = 0; i < numTracks; i++) {
                    var track = seq.audioTracks[i];
                    if (!track) continue;

                    try {
                        // トラックにエフェクトを追加
                        var component = track.addAudioEffect(limiter);

                        if (component && component.properties) {
                            // -1dB設定を試みる
                            // プロパティ名は "Maximum Amplitude" や "Threshold" など
                            for (var p = 0; p < component.properties.numItems; p++) {
                                var prop = component.properties[p];
                                if (!prop) continue;

                                var displayName = prop.displayName ? String(prop.displayName).toLowerCase() : "";

                                // Max, Threshold, Limitなどのキーワードを含むプロパティを探す
                                if (displayName.indexOf("max") !== -1 ||
                                    displayName.indexOf("threshold") !== -1 ||
                                    displayName.indexOf("limit") !== -1) {
                                    try {
                                        // -1dBに設定
                                        prop.setValue(-1.0, true);
                                        log("A" + (i + 1) + " トラックに Hard Limiter (-1dB) を適用");
                                        appliedCount++;
                                        break;
                                    } catch (e) {
                                        log("プロパティ設定エラー: " + e);
                                    }
                                }
                            }
                        }
                    } catch (e) {
                        log("トラック" + (i + 1) + "へのエフェクト適用エラー: " + e);
                    }
                }

                if (appliedCount > 0) {
                    log("Hard Limiterを" + appliedCount + "個のオーディオトラックに適用しました");
                }
            } catch (e) {
                log("ハードリミット適用エラー: " + e);
            }
        }

        // === SRT 書き出し & インポート関数 ===
        function exportSrtAndImport(subs, workDir, seq, sessionBin) {
            if (!subs || !subs.length) {
                log("字幕データが空です。");
                return;
            }

            var dirNum = extractDirNumber(workDir);
            var srtName = "subtitles_" + dirNum + ".srt";
            var srtPath = workDir + "/" + srtName;

            // 時間変換ヘルパー (秒 -> 00:00:00,000)
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

            // 書き出し (明示的な \r\n 使用)
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

                        // f.write を使い、明示的に \r\n を入れる (Mac対策)
                        f.write((i + 1) + "\r\n");
                        f.write(startS + " --> " + endS + "\r\n");
                        f.write(item.text + "\r\n");
                        f.write("\r\n");
                    }
                    f.close();
                    log("SRT export finish: " + srtPath);
                } else {
                    alert("SRTファイルを開けませんでした: " + srtPath);
                    return;
                }
            } catch (e) {
                alert("SRT書き出しエラー: " + e);
                return;
            }

            try {
                // インポート実行
                var ok = app.project.importFiles([f.fsName], 1, sessionBin, 0);
                if (!ok) {
                    log("SRTインポート失敗");
                    return;
                }

                // import 直後は探索で見つからないことがあるため少し待つ
                $.sleep(500);

                // 最新のアイテムを探す
                var srtItem = _findItemByNameInBin(sessionBin, srtName.toLowerCase());
                if (!srtItem) {
                    var noExt = srtName.replace(/\.[^\.]+$/, "");
                    srtItem = _findItemByNameInBin(sessionBin, noExt.toLowerCase());
                }
                if (!srtItem) {
                    // 念のため、プロジェクト全体からも探す
                    srtItem = _findItemByNameInBin(app.project.rootItem, srtName.toLowerCase());
                }
                if (!srtItem) {
                    var noExt2 = srtName.replace(/\.[^\.]+$/, "");
                    srtItem = _findItemByNameInBin(app.project.rootItem, noExt2.toLowerCase());
                }

                if (srtItem && seq) {
                    var placed = placeSubtitleOnTimeline(srtItem, seq);
                    if (!placed) {
                        alert("SRTはインポート済みですが、タイムライン配置に失敗しました。");
                    } else {
                        // Premiere がメディア長を短く認識する場合があるため
                        // キャプションクリップのアウト点を SRT 最終エントリの終端まで強制延長
                        try {
                            var lastEndSec = subs[subs.length - 1].end;
                            $.sleep(300);
                            var capCount = _captionTrackCount(seq);
                            for (var ct = 0; ct < capCount; ct++) {
                                var capTrack = seq.captionTracks[ct];
                                if (!capTrack || !capTrack.clips) continue;
                                for (var cc = 0; cc < capTrack.clips.numItems; cc++) {
                                    var capClip = capTrack.clips[cc];
                                    if (!capClip) continue;
                                    var tCapEnd = new Time();
                                    tCapEnd.seconds = lastEndSec;
                                    capClip.end = tCapEnd;
                                    log("キャプションクリップ延長: -> " + lastEndSec + "s");
                                }
                            }
                        } catch (e) {
                            log("キャプションクリップ延長エラー: " + e);
                        }
                    }
                } else {
                    alert("SRTアイテムをプロジェクト内で見つけられませんでした。");
                }
            } catch (e) {
                alert("SRTインポート/配置処理で例外: " + e);
            }
        }

        // 5) music_time_code_info.txt に開始時刻を書き出す
        // 形式: HH:MM:SS - タイトル
        function getFormattedTimestamps(seq) {
            if (!seq) return [];
            var track = seq.audioTracks[0]; // A1トラック前提
            if (!track) return [];

            var results = [];
            // 秒 → HH:MM:SS
            function hhmmss(sec) {
                sec = Math.round(sec);
                var h = Math.floor(sec / 3600);
                var m = Math.floor((sec % 3600) / 60);
                var s = sec % 60;
                return (h < 10 ? "0" : "") + h + ":" +
                    (m < 10 ? "0" : "") + m + ":" +
                    (s < 10 ? "0" : "") + s;
            }

            // クリップを収集してソート
            var clipList = [];
            for (var i = 0; i < track.clips.numItems; i++) {
                clipList.push(track.clips[i]);
            }
            clipList.sort(function (a, b) {
                return a.start.seconds - b.start.seconds;
            });

            for (var i = 0; i < clipList.length; i++) {
                var c = clipList[i];
                var t = c.start.seconds;
                var name = c.projectItem ? c.projectItem.name : c.name;
                var title = getTitleFromFileName(name);

                results.push(hhmmss(t) + " - " + title);
            }

            return results;
        }

        // ===========================================================
        function run() {
            assertPremiere();

            // === 目標時間をダイアログで取得 ===
            var targetDuration = askTargetDuration();
            if (targetDuration === null) {
                alert("キャンセルされました");
                return;
            }

            var SEQ_NAME = getSequenceNameFromProject();

            var workDir = resolveWorkDir(); // Restored


            // SESSION_BIN_NAME を動的に決定
            var baseNum = extractDirNumber(workDir);
            var SESSION_BIN_NAME = baseNum + "_今回だけの素材";

            var musicMp3 = listMp3s(workDir);                // <project_dir>/music/*.mp3
            if (!musicMp3.length) {
                alert("music フォルダに mp3 が見つからないよ：\n" + (workDir + "/music"));
                return;
            }


            var sessionBin = getOrCreateSessionBin(SESSION_BIN_NAME);

            // 1) mp3 をインポート（既存あれば再利用）＆配列を ProjectItem に差し替え
            var mp3Items = [];
            for (var i = 0; i < musicMp3.length; i++) {

                var item = importIfNeeded(musicMp3[i], sessionBin);
                if (item) mp3Items.push(item);
            }
            if (!mp3Items.length) {
                alert("mp3 をプロジェクトに取り込めなかったみたい…");
                return;
            }

            // 2) プロジェクト名由来のシーケンスを取得/作成（最初の mp3 を種にしてOK）
            var seqResult = ensureSequence(SEQ_NAME, mp3Items[0]);
            var seq = seqResult.seq;
            var isNewSequence = seqResult.isNew;
            if (!seq) {
                alert("シーケンスを取得/作成できませんでした");
                return;
            }
            var a = getWritableAudioTrack(seq);
            if (!a) {
                alert("オーディオトラックが見つからない/ロックされてるかも");
                return;
            }

            // 新規シーケンス作成時は種クリップが自動配置されるため削除
            if (isNewSequence) {
                try {
                    // 自動配置された種クリップを全て削除
                    for (var ci = a.clips.numItems - 1; ci >= 0; ci--) {
                        a.clips[ci].remove(true, true);
                    }
                    // ビデオトラックも同様にクリア
                    for (var vi = 0; vi < seq.videoTracks.numTracks; vi++) {
                        var vt = seq.videoTracks[vi];
                        for (var vci = vt.clips.numItems - 1; vci >= 0; vci--) {
                            vt.clips[vci].remove(true, true);
                        }
                    }
                    $.sleep(300);
                    log("新規シーケンスの種クリップを削除しました");
                } catch (e) {
                    log("種クリップ削除エラー（無視して続行）: " + e);
                }
            }

            // 3) 現在末尾から順に並べる
            var cursor = getAudioTrackEndSec(a) || 0;

            // 字幕(SRT)用データ保持配列
            var subtitleData = []; // { start: number, end: number, text: string }



            // 指定時間まで MP3 をループ配置
            // 字幕データは最初の1セット分のみ記録
            var mp3Index = 0;
            var isFirstSet = true;
            var MAX_ITERATIONS = 100000; // 無限ループ防止
            var iterCount = 0;
            var loopStartTimes = []; // 各ループ開始秒 (2回目以降)

            while (cursor < targetDuration && iterCount < MAX_ITERATIONS) {
                iterCount++;

                // 2回目以降のセット先頭 → ループ開始位置として記録
                if (!isFirstSet && mp3Index === 0) {
                    loopStartTimes.push(cursor);
                }

                var audioItem = mp3Items[mp3Index];
                var title = getTitleFromFileName(audioItem.name);
                var startSec = cursor;

                var placedDur = placeAtAudio(a, audioItem, cursor);
                var endSec = startSec + placedDur;

                // 字幕データは全クリップ分記録 (SRTはループ含む全区間を出力)
                subtitleData.push({
                    start: startSec,
                    end: endSec,
                    text: title
                });

                cursor += placedDur;
                mp3Index++;
                if (mp3Index >= mp3Items.length) {
                    mp3Index = 0;
                    isFirstSet = false;
                }
            }

            // 目標時間を超えたクリップを targetDuration でトリム
            for (var ti = 0; ti < a.clips.numItems; ti++) {
                var tc = a.clips[ti];
                if (tc && tc.end && tc.end.seconds > targetDuration) {
                    try {
                        var tTrim = new Time();
                        tTrim.seconds = targetDuration;
                        tc.end = tTrim;
                        log("クリップをトリム: clip[" + ti + "] -> " + targetDuration + "s");
                    } catch (e) {
                        log("クリップトリムエラー[" + ti + "]: " + e);
                    }
                }
            }
            cursor = targetDuration; // 正確な目標時間にリセット

            // === スペクトラム動画 (audio-spectrum01.mp4) の配置は廃止 ===
            // ユーザー判断により V2 スペクトラム配置・ルミナンスキー処理を全て削除しました
            // (元のロジックは git ステージ時点に保存されています)

            try { seq.setPlayerPosition(new Time(cursor)); } catch (_) { }
            // 70分未満アラートは削除 (ユーザー要望)

            // 4) 画像配置 (vol* と vol*-1)
            //    選択あり: selected_images.json の main / sub[] を優先
            //    選択なし: vol{N} (メイン) と vol{N}-1 (サブ) にフォールバック
            var dirNum = extractDirNumber(workDir);

            // ファイルを探すヘルパー
            function findImageFile(baseNum, suffix) {
                var nameBase = "vol" + baseNum + (suffix || "");
                var png = new File(workDir + "/" + nameBase + ".png");
                if (png.exists) return png;
                var jpg = new File(workDir + "/" + nameBase + ".jpg");
                if (jpg.exists) return jpg;
                return null;
            }
            function fileInWorkDirIfExists(fn) {
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
            // ExtendScript には JSON がないので簡易パーサで main / sub を抽出
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

            var imgMainFile = null;
            var imgSubFiles = [];   // 複数対応 (user-selected)
            var usedSelected = false;

            // ---- 選択ファイル優先 ----
            var selFile = new File(workDir + "/selected_images.json");
            if (selFile.exists) {
                var selTxt = readTextFile(selFile);
                var sel = parseSelectedImagesJson(selTxt);
                if (sel) {
                    var mf = fileInWorkDirIfExists(sel.main);
                    if (mf) imgMainFile = mf;
                    for (var si = 0; si < sel.sub.length; si++) {
                        var sf = fileInWorkDirIfExists(sel.sub[si]);
                        if (sf) imgSubFiles.push(sf);
                    }
                    if (imgMainFile || imgSubFiles.length > 0) {
                        usedSelected = true;
                        log("selected_images.json を使用: main=" + (imgMainFile ? imgMainFile.name : "-") + ", sub=" + imgSubFiles.length + "枚");
                    }
                }
            }

            // ---- フォールバック: 既存の vol{N} / vol{N}-1 ----
            if (!usedSelected) {
                imgMainFile = findImageFile(dirNum, "");
                var imgSubFile = findImageFile(dirNum, "-1");
                if (imgSubFile) imgSubFiles.push(imgSubFile);
            }

            // 以降のコード互換用: imgSubFile は先頭のサブを指す
            var imgSubFile = imgSubFiles.length > 0 ? imgSubFiles[0] : null;

            if (!imgMainFile && !imgSubFile) {
                // 画像なしでも処理を続行（音声と字幕のみ配置）
                // 後から placeImagesOnly モードで追加可能
                log("IMAGES_NONE: 画像ファイルが見つかりません。音声のみ配置します (vol" + dirNum + " / selected_images.json どちらも無し)");
            } else {
                // インポート
                var itemMain = imgMainFile ? importIfNeeded(imgMainFile, sessionBin) : null;
                var itemSub = imgSubFile ? importIfNeeded(imgSubFile, sessionBin) : null;
                // 追加のサブも全てインポートして配列化 (30-End を N等分配置)
                var itemSubs = [];
                for (var ii = 0; ii < imgSubFiles.length; ii++) {
                    var fi = imgSubFiles[ii];
                    var imp = importIfNeeded(fi, sessionBin);
                    if (imp) itemSubs.push(imp);
                }

                // タイムライン配置
                // V1トラックを使用（なければアラート）
                var vTrack = seq.videoTracks.numTracks > 0 ? seq.videoTracks[0] : null;
                if (!vTrack) {
                    alert("ビデオトラックがありません");
                } else {
                    // 画像配置: 3分割ロジック (0-5, 5-30, 30-End)
                    var totalDuration = cursor;
                    var pt1 = 5.0;
                    var pt2 = 30.0;
                    var durationEnd;

                    try {
                        // Helper: 指定区間にクリップを配置して長さを調整
                        var placeSeg = function (item, sTime, eTime) {
                            if (!item) return;
                            if (eTime <= sTime) return;

                            vTrack.overwriteClip(item, sTime);

                            // 配置されたクリップを探す (開始時間が sTime 近辺)
                            var pClip = null;
                            for (var k = 0; k < vTrack.clips.numItems; k++) {
                                var c = vTrack.clips[k];
                                if (Math.abs(c.start.seconds - sTime) < 0.1) {
                                    pClip = c;
                                    break;
                                }
                            }
                            if (pClip) {
                                var tEnd = new Time();
                                tEnd.seconds = eTime;
                                pClip.end = tEnd;
                            }
                        };

                        // 1. 0-5s (Main)
                        if (totalDuration > 0) {
                            var end1 = (totalDuration < pt1) ? totalDuration : pt1;
                            placeSeg(itemMain, 0, end1);
                        }

                        // 2. 5-30s (Sub || Main)
                        if (totalDuration > pt1) {
                            var end2 = (totalDuration < pt2) ? totalDuration : pt2;
                            var it = itemSub || itemMain;
                            placeSeg(it, pt1, end2);
                        }

                        // 3. 30-End: 複数サブがあれば N 等分で順に配置、なければ Sub||Main
                        if (totalDuration > pt2) {
                            // 0-5 区間で使った itemMain 以外のサブ群
                            // 30-End 用には subs 全部を素直に循環させる
                            var subsForTail = itemSubs.length > 0 ? itemSubs : (itemSub ? [itemSub] : (itemMain ? [itemMain] : []));
                            var segCount = subsForTail.length;
                            if (segCount <= 1) {
                                placeSeg(subsForTail[0] || null, pt2, totalDuration);
                            } else {
                                var remaining = totalDuration - pt2;
                                var per = remaining / segCount;
                                for (var qi = 0; qi < segCount; qi++) {
                                    var segStart = pt2 + per * qi;
                                    var segEnd = (qi === segCount - 1) ? totalDuration : (pt2 + per * (qi + 1));
                                    placeSeg(subsForTail[qi], segStart, segEnd);
                                }
                            }
                        }

                        log("Images placed: main=" + (itemMain ? "1" : "0") + ", subs=" + itemSubs.length + ", duration=" + totalDuration);

                    } catch (e) {
                        log("画像分割配置エラー: " + e);
                    }
                }
            }




            // 内部生成とインポート
            exportSrtAndImport(subtitleData, workDir, seq, sessionBin);

            // オーディオトラックにハードリミット適用
            applyHardLimiterToAllAudioTracks(seq);

            // フェードアウト (終了20秒前から)
            applyAudioFadeOut(a, targetDuration, 20);

            // タイムスタンプ生成: 1回目の全曲 + ループ開始マーカー "LOOP"
            function secToHHMMSS(sec) {
                sec = Math.round(sec);
                var h = Math.floor(sec / 3600);
                var m = Math.floor((sec % 3600) / 60);
                var s = sec % 60;
                return (h < 10 ? "0" : "") + h + ":" +
                       (m < 10 ? "0" : "") + m + ":" +
                       (s < 10 ? "0" : "") + s;
            }

            var allEntries = [];
            // 1回目の全曲
            for (var ei = 0; ei < subtitleData.length; ei++) {
                allEntries.push({ sec: subtitleData[ei].start, label: subtitleData[ei].text });
            }
            // ループ開始マーカー
            for (var li = 0; li < loopStartTimes.length; li++) {
                allEntries.push({ sec: loopStartTimes[li], label: "LOOP" });
            }
            // 時刻順ソート
            allEntries.sort(function (a, b) { return a.sec - b.sec; });

            var times = [];
            for (var ti2 = 0; ti2 < allEntries.length; ti2++) {
                times.push(secToHHMMSS(allEntries[ti2].sec) + " - " + allEntries[ti2].label);
            }

            // 書き出し
            if (times.length) {
                saveTimesToTxtByDir(times, workDir);
            }

            // 書き出しは別途 output_mediaencorder.jsx に委譲しました

            // 目標時間を H:MM:SS 表示に変換
            var tdH = Math.floor(targetDuration / 3600);
            var tdM = Math.floor((targetDuration % 3600) / 60);
            var tdS = targetDuration % 60;
            var tdStr = tdH + ":" + (tdM < 10 ? "0" : "") + tdM + ":" + (tdS < 10 ? "0" : "") + tdS;

            // 自動化前提: 完了ダイアログは出さず、ログにだけ出力する
            log("[完了] dir=" + workDir +
                " / 目標=" + tdStr + " (" + targetDuration + "s)" +
                " / 曲数=" + mp3Items.length +
                " / 反復=" + iterCount +
                " / フェード=終了20秒前" +
                " / " + SEQ_NAME + "末尾≈" + Math.round(cursor) + "s");
        }

        try { run(); } catch (e) {
            alert("スクリプトでエラーが発生：\n" + (e && e.message ? e.message : e));
        }
    })();
