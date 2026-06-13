#!/usr/bin/env python3
"""
orzz. Premiere Pro 自動化スクリプト（Premiere Link 版）
- JSX 自動配置くんの実行
- 正確な字幕(SRT)生成
- タイムコード書き出し
- ダッシュボード連携
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# ─── ファイルポーリング方式 (JSX-based) ──────────────────────────────────────
# Premiere Pro 2026 は AppleScript の do javascript を持たないため、
# CEP パネル (Premiere Link) とのファイル通信で JSX を実行する。
# パネル: ~/Library/Application Support/Adobe/CEP/extensions/net.premiere.link/
# 通信: /tmp/pymiere_trigger.json → panel evalScript → /tmp/pymiere_result.json
#       （IPC ファイル名は pymiere_* のまま互換維持）

TRIGGER_FILE = '/tmp/pymiere_trigger.json'
RESULT_FILE  = '/tmp/pymiere_result.json'
PING_FILE    = '/tmp/pymiere_ping.txt'
POLL_TIMEOUT = 60   # 秒


def _cleanup_stale_ipc_files(max_age=300):
    """前回のセッションで残った trigger/result ファイルを削除（ping は除く）。
    古い trigger が残っているとパネル起動直後に誤実行される恐れがある。"""
    for path in (TRIGGER_FILE, RESULT_FILE):
        try:
            if os.path.exists(path):
                age = time.time() - os.path.getmtime(path)
                if age > max_age:
                    os.unlink(path)
                    print(f" 古い IPC ファイルを削除: {path} (age={int(age)}s)")
        except Exception:
            pass


def _is_premiere_running():
    """pgrep で Premiere Pro プロセスを確認"""
    return subprocess.run(['pgrep', '-fi', 'Adobe Premiere Pro'],
                          capture_output=True).returncode == 0


def _get_premiere_app_name():
    """実行中の Premiere Pro アプリ名を動的に取得"""
    r = subprocess.run(['osascript', '-e',
                        'tell application "System Events" to get name of every process'],
                       capture_output=True, text=True)
    for part in r.stdout.split(','):
        name = part.strip()
        if 'Adobe Premiere Pro' in name:
            return name
    return 'Adobe Premiere Pro 2026'


def open_pymiere_panel():
    """Premiere Pro の「ウィンドウ > 拡張機能 > Premiere Link」を AppleScript UI で自動クリック。
    アクセシビリティ権限（システム設定 > プライバシーとセキュリティ > アクセシビリティ）が必要。
    関数名は後方互換のため open_pymiere_panel のまま。"""
    if _panel_alive():
        print(" Premiere Link パネルは既に起動中")
        return True

    app_name = _get_premiere_app_name()
    # メニューラベルは日本語/英語両対応で試みる
    for ext_menu in ['拡張機能', 'Extensions']:
        script = f"""
tell application "System Events"
    tell process "{app_name}"
        set frontmost to true
        delay 0.5
        tell menu bar 1
            tell menu bar item "ウィンドウ"
                tell menu "ウィンドウ"
                    click menu item "{ext_menu}"
                    delay 0.3
                    tell menu "{ext_menu}"
                        click menu item "Premiere Link"
                    end tell
                end tell
            end tell
        end tell
    end tell
end tell
"""
        result = subprocess.run(['osascript'], input=script,
                                capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            # パネルが起動するまで待機
            for _ in range(30):
                time.sleep(0.5)
                if _panel_alive():
                    print(" Premiere Link パネルを自動で開きました")
                    return True
            print("⚠️  パネルをクリックしましたが応答待ちです...")
            return False

    print("❌ UI 自動クリック失敗（アクセシビリティ権限が必要かもしれません）")
    print("   システム設定 > プライバシーとセキュリティ > アクセシビリティ に")
    print("   ターミナル（または実行中のアプリ）を追加してください")
    return False


def _panel_alive():
    """パネルが 30 秒以内に ping ファイルを更新しているか確認。
    Premiere が起動直後や重い処理中だと ping 送信が遅れるため猶予を長めに取る。"""
    if not os.path.exists(PING_FILE):
        return False
    age = time.time() - os.path.getmtime(PING_FILE)
    return age < 30.0


def _file_eval_script(jsx_code, timeout=POLL_TIMEOUT):
    """CEP パネル経由でファイルポーリング実行。
    Premiere で「ウィンドウ > 拡張機能 > Premiere Link」を開いておく必要がある。"""
    if not _panel_alive():
        raise RuntimeError(
            "Premiere Link パネルが応答していません。\n"
            "Premiere Pro の「ウィンドウ > 拡張機能 > Premiere Link」を開いてください。"
        )
    # 古い結果を削除
    if os.path.exists(RESULT_FILE):
        os.unlink(RESULT_FILE)
    # トリガーを書き込む
    with open(TRIGGER_FILE, 'w', encoding='utf-8') as f:
        json.dump({'code': jsx_code, 'ts': time.time()}, f)
    # 結果を待機
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.2)
        if os.path.exists(RESULT_FILE):
            with open(RESULT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            os.unlink(RESULT_FILE)
            if 'error' in data:
                raise Exception(data['error'])
            return data.get('result', '')
    raise TimeoutError(f'JSX 実行タイムアウト ({timeout}s)')


# ─── 接続確認 ─────────────────────────────────────────────────────────────────

def check_pymiere():
    """接続確認。pymiere HTTP → ファイルポーリングの順で試みる。"""
    # 1. pymiere HTTP 方式
    try:
        from pymiere.core import eval_script
        from pymiere import exe_utils
        if not exe_utils.is_premiere_running()[0]:
            print("❌ Premiere Pro が起動していません")
            return False
        result = eval_script('app.project.name')
        print(f" Premiere Pro 接続OK (pymiere): {result}")
        return True
    except Exception as e:
        print(f"⚠️  Pymiere HTTP 未接続 ({e})")

    # 2. ファイルポーリング方式
    if not _is_premiere_running():
        print("❌ Premiere Pro が起動していません")
        return False
    if not _panel_alive():
        print("❌ Premiere Link パネルが未応答です。")
        print("   初回の場合はターミナルで以下を実行後、Premiere を再起動してください:")
        print("   for v in 6 7 8 9 10 11 12; do defaults write \"com.adobe.CSXS.$v\" PlayerDebugMode 1; done")
        return False
    try:
        result = _file_eval_script('app.project.name', timeout=10)
        print(f" Premiere Pro 接続OK (file poll): {result}")
        return True
    except Exception as e2:
        print(f"❌ ファイルポーリング エラー: {e2}")
        return False


def _get_eval_script():
    """pymiere HTTP が使えればそれを、なければファイルポーリング版を返す"""
    try:
        from pymiere.core import eval_script as _pm_eval
        _pm_eval('1+1')
        return _pm_eval
    except Exception:
        return _file_eval_script


def get_project_info():
    """プロジェクト情報を取得"""
    eval_script = _get_eval_script()
    result = eval_script("""
    (function() {
        var p = app.project.path ? String(app.project.path) : "";
        var projFile = new File(p);
        var folder = projFile.parent.fsName;
        var name = app.project.name;
        var folderName = (new Folder(folder)).name;
        var m = String(folderName).match(/^([0-9]+)_/);
        var num = m ? m[1] : "00";
        return name + "|" + folder + "|" + num;
    })()
    """)
    parts = result.split("|")
    return {
        "name": parts[0] if len(parts) > 0 else "",
        "folder": parts[1] if len(parts) > 1 else "",
        "num": parts[2] if len(parts) > 2 else "00",
    }


def _load_channel_luma_key_values(video_folder) -> dict:
    """video_folder の親 (チャンネルフォルダ) の .app_channel_config.json から
    premiere_v2_luminance_key.{threshold, cutoff} を読み込む。
    存在しない/不正なら {} を返す (呼び出し側でデフォルト適用)。"""
    try:
        if not video_folder:
            return {}
        ch_folder = Path(video_folder).parent
        p = ch_folder / ".app_channel_config.json"
        if not p.exists():
            return {}
        cc = json.loads(p.read_text(encoding="utf-8"))
        lk = cc.get("premiere_v2_luminance_key") if isinstance(cc, dict) else None
        if not isinstance(lk, dict):
            return {}
        out = {}
        if isinstance(lk.get("threshold"), (int, float)):
            out["threshold"] = float(lk["threshold"])
        if isinstance(lk.get("cutoff"), (int, float)):
            out["cutoff"] = float(lk["cutoff"])
        return out
    except Exception as e:
        print(f"⚠ luma key config 読み込み失敗: {e}")
        return {}


def run_jsx(target_duration=10800, video_folder=None):
    """自動配置くん JSX を実行（ダイアログなし）。
    自動配置は長時間かかるため、pymiere HTTP ではなくファイルポーリング方式で
    タイムアウトを 30 分まで延長する。

    video_folder: 指定するとそのチャンネル config から luminance key 値を読み込む。
                  None の場合は app.project.path から自動取得する。
    """
    # 長時間 JSX は file_eval_script を直接使う（タイムアウト延長のため）
    LONG_JSX_TIMEOUT = 1800  # 30分

    # JSX はバンドル版を使用（Google Drive 同期不要）
    from jsx_bundle import AUTO_PLACE_JSX
    code = AUTO_PLACE_JSX
    print("JSX: bundled")

    # === V2 ルミナンスキー閾値 (channel config 優先、無ければデフォルト) ===
    # デフォルト: threshold=50 / cutoff=30
    # Audio Spectrum (黒背景に白波形) を抜く実用値だが、阿部さんが Premiere で
    # 実測してから .app_channel_config.json の premiere_v2_luminance_key で調整推奨。
    LUMA_THRESHOLD_DEFAULT = 50.0
    LUMA_CUTOFF_DEFAULT = 30.0
    if not video_folder:
        try:
            info = get_project_info()
            video_folder = info.get("folder") or ""
        except Exception:
            video_folder = ""
    lk = _load_channel_luma_key_values(video_folder)
    luma_threshold = lk.get("threshold", LUMA_THRESHOLD_DEFAULT)
    luma_cutoff = lk.get("cutoff", LUMA_CUTOFF_DEFAULT)
    src = "channel config" if lk else "default"
    print(f"  Luminance Key: threshold={luma_threshold}, cutoff={luma_cutoff} ({src})")

    code = code.replace("XXXLUMATHRESHOLDXXX", f"{luma_threshold}")
    code = code.replace("XXXLUMACUTOFFXXX", f"{luma_cutoff}")

    code = code.replace(
        'var targetDuration = askTargetDuration();',
        f'var targetDuration = {target_duration}; // auto'
    )
    code = code.replace(
        '            if (targetDuration === null) {\n'
        '                alert("キャンセルされました");\n'
        '                return;\n'
        '            }',
        '            // auto mode'
    )

    # JSX内のSRT生成・配置を無効化（Pythonで正確なSRTを後から生成するため）
    code = code.replace(
        'exportSrtAndImport(subtitleData, workDir, seq, sessionBin);',
        '// exportSrtAndImport disabled - Python generates accurate SRT'
    )

    # JSX内のタイムコード書き出しも無効化（Pythonで正確に生成）
    code = code.replace(
        'if (times.length) {\n                saveTimesToTxtByDir(times, workDir);\n            }',
        '// saveTimesToTxtByDir disabled - Python generates accurate timecodes'
    )

    tmp = tempfile.NamedTemporaryFile(suffix='.jsx', delete=False, mode='w', encoding='utf-8')
    tmp.write(code)
    tmp.close()

    print(f"送信中... (目標: {target_duration}秒 = {target_duration//3600}:{(target_duration%3600)//60:02d}:{target_duration%60:02d})")

    try:
        # JSX 文字列リテラル用にパスを安全にエスケープ（バックスラッシュ・引用符対策）
        safe_path = tmp.name.replace('\\', '\\\\').replace('"', '\\"')
        # file_eval_script で timeout を延長（3時間分の自動配置は 60s では足りない）
        _file_eval_script(f'$.evalFile("{safe_path}")', timeout=LONG_JSX_TIMEOUT)
        print(" JSX 実行完了")
        return True
    except Exception as e:
        print(f"❌ JSX 実行エラー: {e}")
        return False
    finally:
        os.unlink(tmp.name)


def get_timeline_clips():
    """タイムラインの全クリップ情報を取得（実際の位置）"""
    eval_script = _get_eval_script()
    result = eval_script("""
    (function() {
        var seq = app.project.activeSequence;
        if (!seq) return "";
        var track = seq.audioTracks[0];
        if (!track) return "";
        var out = [];
        for (var i = 0; i < track.clips.numItems; i++) {
            var c = track.clips[i];
            var name = c.projectItem ? c.projectItem.name : (c.name || "");
            var noExt = String(name).replace(/\\.[^\\.]+$/, "");
            var title = noExt.replace(/^z+_/, "");
            var cleanTitle = title.replace(/.mp3$/i, "").replace(/.wav$/i, "");
            out.push(c.start.seconds.toFixed(3) + "," + c.end.seconds.toFixed(3) + "," + cleanTitle);
        }
        return out.join("\\n");
    })()
    """)
    clips = []
    for line in result.strip().split("\n"):
        if not line:
            continue
        # "\\n" がリテラルで返ることがある
        for subline in line.split("\\n"):
            if not subline:
                continue
            parts = subline.split(",", 2)
            if len(parts) >= 3:
                clips.append({
                    "start": float(parts[0]),
                    "end": float(parts[1]),
                    "title": parts[2],
                })
    clips.sort(key=lambda c: c["start"])
    return clips


def generate_srt(clips, output_path):
    """クリップ情報から正確なSRTファイルを生成"""
    def to_srt_time(sec):
        ms = round(sec * 1000)
        h = ms // 3600000
        ms %= 3600000
        m = ms // 60000
        ms %= 60000
        s = ms // 1000
        ms %= 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(output_path, 'w', encoding='utf-8-sig') as f:
        for i, clip in enumerate(clips):
            if clip["end"] - clip["start"] < 0.001:
                continue
            f.write(f"{i + 1}\r\n")
            f.write(f"{to_srt_time(clip['start'])} --> {to_srt_time(clip['end'])}\r\n")
            f.write(f"{clip['title']}\r\n")
            f.write("\r\n")

    print(f" SRT 生成: {output_path} ({len(clips)} エントリ)")


def generate_timecode(clips, output_path, total_songs=None):
    """タイムコード情報を生成（LOOPマーカー付き）"""
    def to_hhmmss(sec):
        sec = round(sec)
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    # 曲数を検出してループ境界を特定
    if total_songs is None:
        # ユニークなタイトルの数 = 1セット分の曲数
        seen = []
        for c in clips:
            if c["title"] in seen:
                break
            seen.append(c["title"])
        total_songs = len(seen)

    lines = []
    for i, clip in enumerate(clips):
        # ループ境界にマーカーを挿入
        if i > 0 and i % total_songs == 0:
            lines.append(f"{to_hhmmss(clip['start'])} - LOOP")
        lines.append(f"{to_hhmmss(clip['start'])} - {clip['title']}")

    with open(output_path, 'w', encoding='utf-8') as f:
        for line in lines:
            f.write(line + "\n")

    print(f" タイムコード生成: {output_path} ({len(lines)} 行)")


def import_srt_to_timeline(srt_path):
    """SRTファイルをPremiereにインポートしてキャプショントラックに配置"""
    eval_script = _get_eval_script()
    safe_srt = str(srt_path).replace('\\', '\\\\').replace('"', '\\"')

    jsx = f"""
    (function() {{
        var seq = app.project.activeSequence;
        if (!seq) return "no sequence";

        var srtFile = new File("{safe_srt}");
        if (!srtFile.exists) return "srt not found";

        // プロジェクトフォルダから番号取得してビン名決定
        var workDir = (new File(app.project.path)).parent.fsName;
        var dirName = (new Folder(workDir)).name;
        var m = String(dirName).match(/^([0-9]+)_/);
        var num = m ? m[1] : "00";
        var binName = num + "_今回だけの素材";

        // ビンを取得or作成
        var root = app.project.rootItem;
        var bin = null;
        for (var i = 0; i < root.children.numItems; i++) {{
            if (root.children[i].name === binName && root.children[i].type === 2) {{
                bin = root.children[i];
                break;
            }}
        }}
        if (!bin) bin = root.createBin(binName);

        // インポート
        app.project.importFiles([srtFile.fsName], 1, bin, 0);
        $.sleep(500);

        // SRTアイテムを探す
        var srtName = srtFile.name.toLowerCase();
        var srtItem = null;
        function findInBin(b) {{
            for (var i = 0; i < b.children.numItems; i++) {{
                var ch = b.children[i];
                if (ch.type === 2) {{ var hit = findInBin(ch); if (hit) return hit; }}
                else if (ch.name && ch.name.toLowerCase() === srtName) return ch;
            }}
            return null;
        }}
        srtItem = findInBin(root);

        if (!srtItem) return "srt item not found after import";

        // キャプショントラックに配置
        if (seq.createCaptionTrack) {{
            try {{
                seq.createCaptionTrack(srtItem, 0, 2);
                return "ok_caption";
            }} catch(e) {{}}
            try {{
                seq.createCaptionTrack(srtItem, 0);
                return "ok_caption_legacy";
            }} catch(e) {{}}
        }}
        return "caption_track_failed";
    }})()
    """
    result = eval_script(jsx)
    print(f"  SRT配置結果: {result}")
    return "ok" in str(result)


def wait_for_project(expected_name=None, timeout=120):
    """プロジェクトの読み込み完了を待機"""
    eval_script = _get_eval_script()
    print("  プロジェクト読み込み待機中...")
    for i in range(timeout):
        try:
            name = eval_script('app.project.name')
            if name:
                if expected_name is None or expected_name in name:
                    print(f" 読み込み完了: {name}")
                    return True
        except Exception:
            pass
        time.sleep(1)
    print(" タイムアウト")
    return False


def open_project(prproj_path):
    """プロジェクトを開いて読み込み完了を待機"""
    path = Path(prproj_path)
    if not path.exists():
        print(f"❌ プロジェクトファイルが見つかりません: {prproj_path}")
        return False

    jsx_path = str(path).replace('\\', '/').replace('"', '\\"')

    if _panel_alive():
        # Premiere 起動中 + パネル応答中: 既に同じプロジェクトなら何もしない
        try:
            current = _file_eval_script('app.project.name', timeout=5)
            if current and path.stem in current:
                print(f"  既に開いています: {current}")
                return True
        except Exception:
            pass
        # JSX で直接開く（open コマンドより確実）
        print(f"  プロジェクトを開いています (Premiere 起動中): {path.name}")
        try:
            _file_eval_script(f'app.openDocument("{jsx_path}")', timeout=30)
        except Exception:
            pass  # プロジェクト切替でパネルが再起動するため例外は正常
    else:
        # Premiere 未起動 or パネル未応答: OS に .prproj を渡す
        # (Premiere 起動中の場合は前面化 + プロジェクトを自動で開く)
        print(f"  プロジェクトを開いています (open コマンド経由): {path.name}")
        subprocess.Popen(["open", str(path)])

    # プロジェクト読み込みを待機する前に、パネルが応答しているか確認。
    # パネルが未応答なら自動で開きにいく（これをしないと eval_script がずっと失敗する）。
    if not _wait_for_panel(timeout=20):
        print("  ⚠️  Premiere Link パネルが未応答のため、自動で開きに行きます...")
        if not open_pymiere_panel():
            print("  ❌ パネル起動に失敗しました。手動で「ウィンドウ > 拡張機能 > Premiere Link」を開いてください")
            return False
        # パネル起動後に再度待機
        if not _wait_for_panel(timeout=30):
            print("  ❌ パネル応答を確認できませんでした")
            return False

    return wait_for_project(path.stem)


def _wait_for_panel(timeout=20):
    """パネルが応答する（ping を更新する）まで待機"""
    for _ in range(timeout * 2):
        if _panel_alive():
            return True
        time.sleep(0.5)
    return False


def _resolve_orzz_config_dir() -> Path:
    """設定ディレクトリ（v2 配布化対応）"""
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from _app_config import resolve_config_dir as _r
        return _r()
    except Exception:
        return Path.home() / ".config" / "orzz"


def _read_file_prefix() -> str:
    """file_prefix を読む（既定 "vol"）。

    P2-2: APP_CHANNEL_FOLDER env が立っていれば、registry (channels.json) で
    フォルダ → entry を逆引きして該当チャンネルの prefix を返す（複数チャンネル
    並列実行で UI active channel の prefix が混入するのを防ぐ）。"""
    import os as _os
    import json as _j
    import re as _re
    cfg_dir = _resolve_orzz_config_dir()
    # 優先 1: env override + registry
    env_folder = (_os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
    env_id = (_os.environ.get("APP_CHANNEL_ID") or "").strip()
    if env_folder or env_id:
        try:
            chs_path = cfg_dir / "channels.json"
            if chs_path.exists():
                chs = _j.loads(chs_path.read_text(encoding="utf-8")) or []
                target = ""
                if env_folder:
                    try:
                        target = str(Path(env_folder).expanduser().resolve())
                    except Exception:
                        target = env_folder
                for ch in chs:
                    if env_id and ch.get("id") == env_id:
                        raw = (ch.get("prefix") or "vol").strip()
                        return _re.sub(r"[^A-Za-z0-9_-]+", "", raw) or "vol"
                    if target:
                        try:
                            p = str(Path(ch.get("folder") or "").expanduser().resolve())
                            if p == target:
                                raw = (ch.get("prefix") or "vol").strip()
                                return _re.sub(r"[^A-Za-z0-9_-]+", "", raw) or "vol"
                        except Exception:
                            continue
        except Exception:
            pass
    # 優先 2: global dashboard_config の file_prefix（後方互換）
    try:
        cfg_path = cfg_dir / "dashboard_config.json"
        if cfg_path.exists():
            d = _j.loads(cfg_path.read_text(encoding="utf-8"))
            raw = (d.get("file_prefix") or "vol").strip()
            return _re.sub(r"[^A-Za-z0-9_-]+", "", raw) or "vol"
    except Exception:
        pass
    return "vol"


def save_project(timeout: int = 180) -> bool:
    """現在開いているプロジェクトを保存。
    AME に投入する前に呼ぶことでメディアの再リンクを prproj に反映させ、
    クラウド DL 後の状態で書き出されるようにする。"""
    eval_script = _get_eval_script()
    try:
        result = eval_script("""
        (function() {
            try {
                if (!app.project) return "no_project";
                app.project.save();
                return "ok";
            } catch (e) {
                return "error|" + e;
            }
        })()
        """, timeout=timeout)
    except Exception as e:
        print(f"  ⚠️ プロジェクト保存例外: {e}")
        return False
    ok = "ok" in str(result)
    if ok:
        print(" プロジェクト保存完了")
    else:
        print(f"  ⚠️ プロジェクト保存失敗: {result}")
    return ok


def export_to_media_encoder(output_path: Optional[str] = None):
    """YouTube最適化プリセットでMedia Encoderにキュー送信。
    output_path が指定されればそのフルパスへ書き出す（外部 SSD 用）。
    指定無しなら従来通り、prproj のあるフォルダ内に {prefix}_vol{N}.mp4 として出力。
    """
    eval_script = _get_eval_script()

    preset_path = str(_resolve_orzz_config_dir() / "YouTube_1080p_Optimized.epr")
    if not Path(preset_path).exists():
        bundled = Path(__file__).parent / "YouTube_1080p_Optimized.epr"
        if bundled.exists():
            preset_path = str(bundled)
        else:
            print("❌ YouTube プリセットが見つかりません。setup.sh を実行してください。")
            return False
    safe_preset = preset_path.replace('\\', '\\\\').replace('"', '\\"')
    prefix = _read_file_prefix()
    explicit_output = ""
    if output_path:
        # output 親フォルダを確実に作っておく（AME はディレクトリを作成しない）
        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"❌ 書き出し先フォルダ作成失敗: {output_path} ({e})")
            return False
        explicit_output = output_path.replace('\\', '\\\\').replace('"', '\\"')

    result = eval_script(f"""
    (function() {{
        // .prproj 名と同名のシーケンスを優先 → 無ければ最長シーケンス → 最後に activeSequence
        // (activeSequence 依存だと「文字配置」など短い別シーケンスが書き出される事故あり)
        var targetName = "";
        try {{
            var pn = String(app.project.name || "").replace(/\\.prproj$/i, "");
            targetName = pn || "";
        }} catch (_) {{ }}
        var seq = null;
        var seqs = app.project.sequences;
        var maxDurSec = -1;
        var longest = null;
        for (var i = 0; i < seqs.numSequences; i++) {{
            var s = seqs[i];
            if (!s) continue;
            if (targetName && s.name === targetName) {{ seq = s; break; }}
            var endSec = 0;
            try {{ endSec = (s.end && s.end.seconds) ? s.end.seconds : (parseFloat(s.end)/254016000000); }} catch (_) {{ }}
            try {{
                if (s.audioTracks) {{
                    for (var ti = 0; ti < s.audioTracks.numTracks; ti++) {{
                        var tr = s.audioTracks[ti];
                        if (tr && tr.clips) {{
                            for (var ci = 0; ci < tr.clips.numItems; ci++) {{
                                var cl = tr.clips[ci];
                                if (cl && cl.end && cl.end.seconds > endSec) endSec = cl.end.seconds;
                            }}
                        }}
                    }}
                }}
            }} catch (_) {{ }}
            if (endSec > maxDurSec) {{ maxDurSec = endSec; longest = s; }}
        }}
        if (!seq) seq = longest || app.project.activeSequence;
        if (!seq) return "no sequence";
        var pickedName = "";
        try {{ pickedName = String(seq.name || ""); }} catch (_) {{ }}
        var workDir = (new File(app.project.path)).parent.fsName.replace(/\\\\\\\\/g, "/");
        var dirName = (new Folder(workDir)).name;
        var m = String(dirName).match(/^([0-9]+)_/);
        var num = m ? m[1] : "00";
        var explicitOut = "{explicit_output}";
        var outputPath = explicitOut.length > 0 ? explicitOut : (workDir + "/{prefix}_vol" + num + ".mp4");
        var presetPath = "{safe_preset}";
        var dbg = "";
        try {{
            if (!app.encoder) {{ return "no_encoder|" + pickedName; }}
            dbg += "launch;";
            try {{ app.encoder.launchEncoder(); }} catch (eL) {{
                return "launch_throw|" + pickedName + "|" + eL + "|dbg=" + dbg;
            }}
            $.sleep(3000);
            dbg += "encode;";
            var ok = false;
            try {{
                ok = app.encoder.encodeSequence(seq, outputPath, presetPath, 0, 1);
            }} catch (eE) {{
                return "encode_throw|" + pickedName + "|" + eE + "|dbg=" + dbg;
            }}
            if (!ok) {{
                return "enqueue_failed|" + pickedName + "|encodeSequence returned " + ok + "|dbg=" + dbg;
            }}
            dbg += "startBatch;";
            try {{
                app.encoder.startBatch();
            }} catch (eB) {{
                return "startBatch_throw|" + pickedName + "|" + eB + "|dbg=" + dbg;
            }}
            return "ok|" + pickedName + "|" + outputPath;
        }} catch(e) {{
            return "error|" + e + "|dbg=" + dbg;
        }}
    }})()
    """)
    result_str = str(result or "")
    print(f"  書き出し結果: {result_str}")
    # 「ok|」で始まる場合のみ成功（"ok" in str だと部分一致で誤検知する）
    return result_str.startswith("ok|")


def run_full(target_duration=10800, prproj_path=None, auto_export=False):
    """フルワークフロー: プロジェクトオープン → JSX実行 → 正確なSRT生成 → タイムコード生成"""
    prefix = _read_file_prefix()
    print("=" * 50)
    print("  Premiere 自動配置")
    print("=" * 50)

    # プロジェクト指定がある場合は先に開く（Premiere 未起動でも自動起動）
    if prproj_path:
        if not open_project(prproj_path):
            return False

    # 接続確認（プロジェクトオープン・パネル起動後）
    if not check_pymiere():
        return False

    info = get_project_info()
    print(f"  プロジェクト: {info['name']}")
    print(f"  フォルダ: {info['folder']}")
    print(f"  Vol: {info['num']}")
    print(f"  目標時間: {target_duration}秒")
    print("=" * 50)

    # 1. JSX 実行
    print("\n[1/4] JSX 自動配置実行中...")
    if not run_jsx(target_duration, video_folder=info.get("folder")):
        return False

    # 2. タイムラインからクリップ情報取得
    print("\n[2/4] タイムラインのクリップ情報を取得中...")
    clips = get_timeline_clips()
    if not clips:
        print("❌ クリップが見つかりません")
        return False
    print(f"  {len(clips)} クリップを検出")

    # 3. 正確なSRT生成
    print("\n[3/5] 字幕ファイル生成中...")
    folder = info["folder"]
    num = info["num"]
    srt_path = os.path.join(folder, f"subtitles_{num}.srt")
    generate_srt(clips, srt_path)

    # 4. タイムコード生成
    print("\n[4/5] タイムコード生成中...")
    tc_path = os.path.join(folder, f"music_time_code_info_{num}.txt")
    generate_timecode(clips, tc_path)

    # 5. SRTをPremiereにインポート → キャプショントラックに配置
    steps = "6" if auto_export else "5"
    print(f"\n[5/{steps}] 字幕をタイムラインに配置中...")
    import_srt_to_timeline(srt_path)

    # 6. 書き出し（オプション）
    if auto_export:
        print(f"\n[6/6] YouTube最適化プリセットで書き出し中...")
        export_to_media_encoder()

    print("\n" + "=" * 50)
    print(" 完了")
    print(f"  SRT: subtitles_{num}.srt")
    print(f"  TC:  music_time_code_info_{num}.txt")
    if auto_export:
        print(f"  MP4: {prefix}_vol{num}.mp4 (Media Encoder で書き出し中)")
    print("=" * 50)

    return True


def regenerate_subtitles():
    """タイムラインの現在状態から字幕とタイムコードだけ再生成"""
    print("字幕・タイムコード再生成...")

    if not check_pymiere():
        return False

    info = get_project_info()
    clips = get_timeline_clips()
    if not clips:
        print("❌ クリップが見つかりません")
        return False

    folder = info["folder"]
    num = info["num"]

    srt_path = os.path.join(folder, f"subtitles_{num}.srt")
    tc_path = os.path.join(folder, f"music_time_code_info_{num}.txt")

    generate_srt(clips, srt_path)
    generate_timecode(clips, tc_path)

    print(f" {len(clips)} クリップ分の字幕を再生成しました")
    return True


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="orzz. Premiere 自動化")
    parser.add_argument("--duration", "-d", type=int, default=10800,
                        help="目標時間（秒）デフォルト: 10800 (3時間)")
    parser.add_argument("--project", "-p", type=str, default=None,
                        help="プロジェクトファイルパス（指定すると自動で開く）")
    parser.add_argument("--export", "-e", action="store_true",
                        help="完了後にMedia Encoderへ書き出しキューを送信")
    parser.add_argument("--export-only", action="store_true",
                        help="書き出しのみ実行")
    parser.add_argument("--output-path", type=str, default=None,
                        help="書き出し先フルパス（外部 SSD 用。未指定なら prproj フォルダ内）")
    parser.add_argument("--open-wait", type=int, default=10,
                        help="--export-only 時、open 後にクラウド DL とメディア再リンクを待つ秒数（既定 10）")
    parser.add_argument("--regenerate-srt", action="store_true",
                        help="字幕・タイムコードだけ再生成")
    parser.add_argument("--check", action="store_true",
                        help="接続確認のみ")
    args = parser.parse_args()

    _cleanup_stale_ipc_files()

    if args.check:
        check_pymiere()
    elif args.regenerate_srt:
        regenerate_subtitles()
    elif args.export_only:
        if args.project:
            if not open_project(args.project):
                sys.exit(1)
            # クラウド DL（Drive ストリーム）とメディア再リンクを待つ。
            # open 直後に encodeSequence するとリンク未解決のまま AME 投入され、
            # 失敗 → ウォッチャー再検知で重複登録、という事故が起きる。
            wait = max(0, int(args.open_wait or 0))
            if wait > 0:
                print(f"  メディア解決待機: {wait}秒")
                time.sleep(wait)
        if check_pymiere():
            # メディア紐づけを確定させてから AME に渡す。
            # 保存に失敗したら未保存状態で AME に投入することになり、次回の openDocument で
            # 「変更を保存しますか？」モーダルが出て JSX がハングする → fail-fast で abort。
            if not save_project():
                print("❌ プロジェクト保存に失敗。未保存のまま進めると次回ハングするため中止します。")
                sys.exit(2)
            print("YouTube最適化プリセットで書き出し...")
            ok = export_to_media_encoder(output_path=args.output_path)
            sys.exit(0 if ok else 1)
        sys.exit(1)
    else:
        run_full(args.duration, prproj_path=args.project, auto_export=args.export)
