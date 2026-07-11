#!/usr/bin/env python3
"""
Photoshop Link 連携ラッパ（AppleScript do javascript 版・PS 2024+）

PS 2026 (v27.5) 以降は CEP 拡張機能が削除され、UXP は /tmp ファイル IPC に
sandbox 制約があるため、AppleScript の `do javascript` 経由で ExtendScript を
直接実行する方式を採用。プラグイン不要・PS 標準サポート（25+ 年枯れた API）。

実行モデル:
    Python → osascript -e 'tell app "Adobe Photoshop 2026" to do javascript file ...'
           → PS 内で ExtendScript 評価 → 最終式の値を AppleScript stdout で受信

JSX コードは temp ファイル経由で渡すため、文字列エスケープ不要。
複雑な戻り値は JSX 末尾で JSON.stringify(...) すれば Python 側で自動 json.loads。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from resource_lock import ResourceBusyError, ResourceLock

DEFAULT_TIMEOUT = 240  # JSX 実行のデフォルトタイムアウト（秒）。重い PSD 合成（スマートオブジェクト流し込み＋2枚書き出し）に対応

_cached_app_name: Optional[str] = None
_cached_app_bundle: Optional[Path] = None

# ExtendScript には JSON が無いので polyfill（最小実装）。
# Douglas Crockford の json2.js から stringify のみ。
_JSON_POLYFILL = r"""
if (typeof JSON === "undefined") { JSON = {}; }
if (typeof JSON.stringify !== "function") {
    var __cx = /[\x00-\x1f-]/g;
    var __esc = {'\b':'\\b','\t':'\\t','\n':'\\n','\f':'\\f','\r':'\\r','"':'\\"','\\':'\\\\'};
    function __qstr(s) {
        __cx.lastIndex = 0;
        var r = '"';
        for (var i = 0; i < s.length; i++) {
            var c = s.charAt(i);
            if (__esc[c]) r += __esc[c];
            else if (c < ' ') r += '\\u' + ('0000' + c.charCodeAt(0).toString(16)).slice(-4);
            else r += c;
        }
        return r + '"';
    }
    function __ser(v) {
        if (v === null || v === undefined) return "null";
        var t = typeof v;
        if (t === "string") return __qstr(v);
        if (t === "number") return isFinite(v) ? String(v) : "null";
        if (t === "boolean") return String(v);
        if (v instanceof Array) {
            var parts = [];
            for (var i = 0; i < v.length; i++) parts.push(__ser(v[i]));
            return "[" + parts.join(",") + "]";
        }
        if (t === "object") {
            var parts = [];
            for (var k in v) {
                if (v.hasOwnProperty(k)) parts.push(__qstr(k) + ":" + __ser(v[k]));
            }
            return "{" + parts.join(",") + "}";
        }
        return "null";
    }
    JSON.stringify = function(v) { return __ser(v); };
}
""".strip()


# ─── 低レベル: AppleScript do javascript 実行 ───────────────────────────────

def _is_photoshop_running() -> bool:
    return subprocess.run(["pgrep", "-fi", "Adobe Photoshop"],
                          capture_output=True, timeout=10).returncode == 0


def _find_photoshop_app_bundle() -> Optional[Path]:
    """Installed Photoshop.app を探す（2024/2025/2026 などの名前差を吸収）。"""
    global _cached_app_bundle
    if _cached_app_bundle and _cached_app_bundle.exists():
        return _cached_app_bundle

    candidates: list[Path] = []
    apps = Path("/Applications")
    patterns = [
        "Adobe Photoshop*/Adobe Photoshop*.app",
        "Adobe Photoshop*.app",
    ]
    for pattern in patterns:
        candidates.extend(apps.glob(pattern))

    if not candidates:
        try:
            r = subprocess.run(
                ["mdfind", 'kMDItemKind == "Application" && kMDItemDisplayName == "*Photoshop*"'],
                capture_output=True, text=True, timeout=8,
            )
            candidates.extend(Path(x.strip()) for x in r.stdout.splitlines() if x.strip())
        except Exception:
            pass

    candidates = [p for p in candidates if p.exists() and p.suffix == ".app"]
    if not candidates:
        return None

    # バージョン付きは新しいものを優先。名前に数字が無いものは最後。
    def version_key(path: Path) -> tuple[int, str]:
        nums = [int(x) for x in re.findall(r"20\d{2}|\d+(?:\.\d+)?", path.name)]
        return (max(nums) if nums else 0, path.name)

    _cached_app_bundle = sorted(candidates, key=version_key, reverse=True)[0]
    return _cached_app_bundle


def ensure_photoshop_running(wait_sec: float = 20.0) -> bool:
    """Photoshop が未起動ならインストール済み app を探して起動する。"""
    if _is_photoshop_running():
        return True
    if os.environ.get("APP_PHOTOSHOP_AUTO_LAUNCH", "1").strip().lower() in ("0", "false", "no"):
        return False

    bundle = _find_photoshop_app_bundle()
    if not bundle:
        return False
    print(f" Photoshop 未起動 → 起動します: {bundle.name}")
    try:
        subprocess.run(["open", "-a", str(bundle)], check=False, capture_output=True, text=True, timeout=10)
    except Exception as e:
        print(f"  ⚠ Photoshop 起動コマンド失敗: {e}")
        return False

    deadline = time.time() + max(1.0, wait_sec)
    while time.time() < deadline:
        if _is_photoshop_running():
            # AppleScript の application process 登録が一拍遅れることがある。
            time.sleep(1.5)
            return True
        time.sleep(0.5)
    return _is_photoshop_running()


def _get_photoshop_app_name() -> str:
    """実行中の Photoshop アプリ名を動的取得（バージョン違いに対応）。"""
    global _cached_app_name
    if _cached_app_name and _is_photoshop_running():
        return _cached_app_name
    r = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to get name of every process'],
        capture_output=True, text=True, timeout=10,
    )
    for part in r.stdout.split(","):
        name = part.strip()
        if "Adobe Photoshop" in name:
            _cached_app_name = name
            return name
    return "Adobe Photoshop 2026"


def run_jsx(code: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """ExtendScript (JSX) を Photoshop で実行し最終式の値を返す。

    code 例:
        "app.documents.length ? app.activeDocument.name : 'no_doc'"

    末尾を JSON.stringify(...) にすると Python 側で json.loads した値を返す。
    """
    resource = ResourceLock("photoshop", owner="cli:app_photoshop").acquire(blocking=False)
    try:
        return _run_jsx_locked(code, timeout)
    finally:
        resource.release()


def _run_jsx_locked(code: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
    """Photoshop resource lock 保持中の JSX 実行本体。"""
    if not ensure_photoshop_running():
        raise RuntimeError("Photoshop が起動していません")

    app_name = _get_photoshop_app_name()
    # 一時ファイルに JSX を書き出して do javascript file で読ませる
    # （文字列エスケープ問題を完全回避）。JSON polyfill を冒頭に挿入。
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsx", delete=False, encoding="utf-8") as tf:
        tf.write(code)
        jsx_path = tf.name

    try:
        # AppleScript の Apple event タイムアウト（既定 ~120s）も延ばす。
        # これが無いと重い PSD 合成で subprocess より先に AppleScript 側が切れる。
        applescript = (
            f'set jsxFile to (POSIX file "{jsx_path}")\n'
            f'with timeout of {int(timeout) + 60} seconds\n'
            f'tell application "{app_name}"\n'
            f'    do javascript file jsxFile\n'
            f'end tell\n'
            f'end timeout'
        )
        proc = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or "unknown error"
            raise RuntimeError(f"JSX error: {err}")
        result = proc.stdout.strip()
        # JSON っぽければパース（list_layers などの戻り値）
        if result.startswith(("{", "[")):
            try:
                return json.loads(result)
            except (ValueError, TypeError):
                pass
        return result
    finally:
        try:
            os.unlink(jsx_path)
        except Exception:
            pass


# 後方互換エイリアス
run_uxp = run_jsx


# ─── 高レベル PSD 操作 ───────────────────────────────────────────────────────

def check_photoshop() -> dict:
    """接続状態を返す。"""
    if not _is_photoshop_running():
        return {"connected": False, "reason": "Photoshop が起動していません"}
    try:
        name = run_jsx(
            'app.documents.length ? app.activeDocument.name : "no_doc";',
            timeout=10,
        )
        return {
            "connected": True,
            "active_document": name if name != "no_doc" else None,
            "app_name": _get_photoshop_app_name(),
        }
    except Exception as e:
        return {"connected": False, "reason": str(e)}


def open_psd(path: str) -> str:
    """PSD を開いてアクティブドキュメント名を返す。"""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"PSD が存在しません: {p}")
    # ExtendScript: File() で開く
    jsx_path = json.dumps(str(p))  # クォート + エスケープ済み
    code = f"""
        var f = new File({jsx_path});
        if (!f.exists) {{ "ERROR: file not found"; }}
        else {{ app.open(f); app.activeDocument.name; }}
    """
    result = run_jsx(code, timeout=240)
    if isinstance(result, str) and result.startswith("ERROR:"):
        raise RuntimeError(result)
    return result


def list_layers() -> list:
    """アクティブドキュメントのレイヤー（グループは再帰展開）を {name, kind, path} で返す。

    ExtendScript には JSON が無いのでタブ区切りテキストを返させて Python 側でパース。
    """
    code = """
        var __result;
        if (!app.documents.length) { __result = ""; }
        else {
            var out = [];
            var kindStr = function(l) {
                try {
                    if (l.layers !== undefined) return "GROUP";
                    if (l.kind) return String(l.kind);
                    return "?";
                } catch(e) { return "?"; }
            };
            var walk = function(layers, prefix) {
                for (var i = 0; i < layers.length; i++) {
                    var l = layers[i];
                    var p = prefix ? (prefix + "/" + l.name) : l.name;
                    out.push(kindStr(l) + "\\t" + p);
                    if (l.layers && l.layers.length) walk(l.layers, p);
                }
            };
            walk(app.activeDocument.layers, "");
            __result = out.join("\\n");
        }
        __result;
    """
    raw = run_jsx(code, timeout=15)
    if not raw or not isinstance(raw, str):
        return []
    layers = []
    for line in raw.split("\n"):
        if "\t" not in line:
            continue
        kind, path = line.split("\t", 1)
        layers.append({"name": path.split("/")[-1], "kind": kind, "path": path})
    return layers


def set_text(layer_name: str, text: str, centered: bool = False,
             font: Optional[str] = None) -> bool:
    """テキストレイヤーを差し替える（グループ内も再帰検索）。

    Args:
        layer_name: 対象テキスト層名
        text: 差し替え後のテキスト
        centered: True にすると、テキスト揃えを CENTER + 水平方向のみキャンバス中央に translate（縦位置は維持）
        font: PostScript フォント名（例: "HelveticaNeue-UltraLight"）。指定時は
              contents 差し替え後に textItem.font を明示セットする（contents 書き換えで
              フォントが意図せずリセット/fallback されるケースへの保険）。
              システムに無いフォント名なら警告のみで non-fatal。
    """
    name_js = json.dumps(layer_name)
    text_js = json.dumps(text)
    centered_js = "true" if centered else "false"
    font_js = json.dumps(font) if font else "null"
    code = f"""
        if (!app.documents.length) {{ "ERROR: no active doc"; }}
        else (function() {{
            var wantName = {name_js};
            var wantText = {text_js};
            var doCenter = {centered_js};
            var wantFont = {font_js};
            function find(layers) {{
                for (var i = 0; i < layers.length; i++) {{
                    var l = layers[i];
                    if (l.name === wantName) return l;
                    if (l.layers && l.layers.length) {{
                        var r = find(l.layers);
                        if (r) return r;
                    }}
                }}
                return null;
            }}
            var t = find(app.activeDocument.layers);
            if (!t) return "ERROR: layer not found: " + wantName;
            if (t.kind && String(t.kind) !== "LayerKind.TEXT") return "ERROR: not a text layer: " + t.name + " (" + t.kind + ")";
            t.textItem.contents = wantText;
            if (wantFont) {{
                try {{ t.textItem.font = wantFont; }} catch (fe) {{ /* non-fatal */ }}
            }}
            if (doCenter) {{
                try {{
                    // テキスト揃え: CENTER
                    t.textItem.justification = Justification.CENTER;
                    // 水平方向のみキャンバス中央へ translate（縦位置は維持）
                    var b = t.bounds; // [left, top, right, bottom] UnitValue
                    var textCenterX = (b[0].as("px") + b[2].as("px")) / 2;
                    var docCenterX = app.activeDocument.width.as("px") / 2;
                    var dx = docCenterX - textCenterX;
                    if (Math.abs(dx) > 0.5) t.translate(dx, 0);
                }} catch (e) {{ return "ERROR: center failed: " + e; }}
            }}
            return "ok";
        }})();
    """
    result = run_jsx(code, timeout=20)
    if isinstance(result, str) and result.startswith("ERROR:"):
        raise RuntimeError(result)
    return result == "ok"


def export_image(out_path: str, fmt: str = "jpg", quality: int = 90,
                 target_width: Optional[int] = None,
                 target_height: Optional[int] = None) -> str:
    """アクティブドキュメントを JPG / PNG として保存して絶対パスを返す。

    target_width / target_height を両方指定すると、書き出し直前にドキュメントを
    duplicate して指定サイズに resize（BICUBICSMOOTHER）してから書き出す。
    元ドキュメントは手付かずなので、後続の set_text / visibility 切替が継続可能。
    PSD キャンバスが 1280×720 で出力を 1920×1080 にしたい等のケースに使う。
    """
    fmt = fmt.lower()
    if fmt not in ("jpg", "jpeg", "png"):
        raise ValueError("fmt は 'jpg' か 'png' のみ")
    out = Path(out_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    out_js = json.dumps(str(out))
    if fmt in ("jpg", "jpeg"):
        opts_jsx = f"""
            var opts = new JPEGSaveOptions();
            opts.quality = {min(12, max(1, int(quality / 10)))};
            opts.embedColorProfile = true;
        """
    else:
        opts_jsx = """
            var opts = new PNGSaveOptions();
            opts.compression = 6;
        """

    use_resize = target_width and target_height and target_width > 0 and target_height > 0
    if use_resize:
        tw = int(target_width)
        th = int(target_height)
        body_jsx = f"""
            var src = app.activeDocument;
            var dup = src.duplicate("__export_resize__", true);
            app.activeDocument = dup;
            try {{
                dup.resizeImage(UnitValue({tw}, "px"), UnitValue({th}, "px"), null, ResampleMethod.BICUBICSMOOTHER);
                var f = new File({out_js});
                {opts_jsx}
                dup.saveAs(f, opts, true, Extension.LOWERCASE);
                var fs = f.fsName;
                dup.close(SaveOptions.DONOTSAVECHANGES);
                app.activeDocument = src;
                fs;
            }} catch(e) {{
                try {{ dup.close(SaveOptions.DONOTSAVECHANGES); }} catch(_e) {{}}
                app.activeDocument = src;
                "ERROR: " + e.message;
            }}
        """
    else:
        body_jsx = f"""
            var f = new File({out_js});
            {opts_jsx}
            app.activeDocument.saveAs(f, opts, true, Extension.LOWERCASE);
            f.fsName;
        """

    code = f"""
        if (!app.documents.length) {{ "ERROR: no active doc"; }}
        else (function() {{
            {body_jsx}
        }})();
    """
    result = run_jsx(code, timeout=240)
    if isinstance(result, str) and result.startswith("ERROR:"):
        raise RuntimeError(result)
    return result


_REPLACE_SO_TEMPLATE = """
    var __r;
    if (!app.documents.length) { __r = "ERROR: no active doc"; }
    else {
        var wantName = __NAME_JS__;
        var imagePath = __IMG_JS__;
        var doFit = __FIT_JS__;
        var find = function(layers) {
            for (var i = 0; i < layers.length; i++) {
                var l = layers[i];
                if (l.name === wantName) return l;
                if (l.layers && l.layers.length) {
                    var r = find(l.layers);
                    if (r) return r;
                }
            }
            return null;
        };
        var t = find(app.activeDocument.layers);
        if (!t) { __r = "ERROR: layer not found: " + wantName; }
        else {
            try {
                app.activeDocument.activeLayer = t;
                var desc = new ActionDescriptor();
                desc.putPath(charIDToTypeID("null"), new File(imagePath));
                desc.putInteger(charIDToTypeID("PgNm"), 1);
                executeAction(stringIDToTypeID("placedLayerReplaceContents"), desc, DialogModes.NO);

                if (doFit) {
                    var doc = app.activeDocument;
                    var docW = doc.width.as("px");
                    var docH = doc.height.as("px");
                    var b = t.bounds;
                    var soW = b[2].as("px") - b[0].as("px");
                    var soH = b[3].as("px") - b[1].as("px");
                    var scale = Math.max(docW / soW, docH / soH) * 100;
                    var soCx = (b[0].as("px") + b[2].as("px")) / 2;
                    var soCy = (b[1].as("px") + b[3].as("px")) / 2;
                    var dx = (docW / 2) - soCx;
                    var dy = (docH / 2) - soCy;
                    t.translate(dx, dy);
                    t.resize(scale, scale, AnchorPosition.MIDDLECENTER);
                }
                __r = "ok";
            } catch(e) {
                __r = "ERROR: " + e.message + " (layer may not be a smart object)";
            }
        }
    }
    __r;
"""


def replace_smart_object(layer_name: str, image_path: str, fit: bool = True) -> bool:
    """指定レイヤー（スマートオブジェクト）の中身を画像で差し替える。

    fit=True の場合、差し替え後にスマートオブジェクトを document 全体に
    フィットするようサイズを再調整する（「全体に配置」の意図）。
    """
    img = Path(image_path).expanduser().resolve()
    if not img.exists():
        raise FileNotFoundError(f"差し替え画像が存在しません: {img}")

    code = (
        _REPLACE_SO_TEMPLATE
        .replace("__NAME_JS__", json.dumps(layer_name))
        .replace("__IMG_JS__", json.dumps(str(img)))
        .replace("__FIT_JS__", "true" if fit else "false")
    )
    result = run_jsx(code, timeout=240)
    if isinstance(result, str) and result.startswith("ERROR:"):
        raise RuntimeError(result)
    return result == "ok"


def set_layer_visible(layer_name: str, visible: bool) -> bool:
    """指定レイヤーの表示/非表示を切り替える（グループ内も再帰検索）。"""
    name_js = json.dumps(layer_name)
    vis_js = "true" if visible else "false"
    code = f"""
        if (!app.documents.length) {{ "ERROR: no active doc"; }}
        else (function() {{
            var wantName = {name_js};
            function find(layers) {{
                for (var i = 0; i < layers.length; i++) {{
                    var l = layers[i];
                    if (l.name === wantName) return l;
                    if (l.layers && l.layers.length) {{
                        var r = find(l.layers);
                        if (r) return r;
                    }}
                }}
                return null;
            }}
            var t = find(app.activeDocument.layers);
            if (!t) return "ERROR: layer not found: " + wantName;
            t.visible = {vis_js};
            return "ok";
        }})();
    """
    result = run_jsx(code, timeout=15)
    if isinstance(result, str) and result.startswith("ERROR:"):
        raise RuntimeError(result)
    return result == "ok"


def _hide_top_level_except(keep_layer: str) -> list:
    """トップレベルレイヤーのうち keep_layer 以外の可視レイヤーを非表示にし、
    非表示にしたレイヤーの **インデックス** のリストを返す（背景=base のみ出力 → 後で復元）。

    名前ではなく index を返すのは、復元（_show_top_level_by_index）で名前マッチングを
    避けるため。SUKIMA のタグライン層 'cozy tune / morning cafe / bossa ＆ soul '
    （末尾スペース・全角＆を含む）のように set_layer_visible の完全一致が壊れる層でも、
    トップレベル index なら確実に復元できる。hide → export → restore の間に
    レイヤーの増減・並べ替えは起きない前提（このコンテキストでは成立する）。
    """
    keep_js = json.dumps(keep_layer)
    code = f"""
        if (!app.documents.length) {{ "ERROR: no active doc"; }}
        else (function() {{
            var keep = {keep_js};
            var hidden = [];
            var ls = app.activeDocument.layers;
            for (var i = 0; i < ls.length; i++) {{
                if (ls[i].name !== keep && ls[i].visible) {{
                    ls[i].visible = false;
                    hidden.push(i);
                }}
            }}
            return hidden.join(",");
        }})();
    """
    result = run_jsx(code, timeout=20)
    if isinstance(result, str) and result.startswith("ERROR:"):
        raise RuntimeError(result)
    if not result:
        return []
    return [int(n) for n in str(result).split(",") if n.strip() != ""]


def _show_top_level_by_index(indices: list) -> bool:
    """トップレベルレイヤーをインデックスで再表示する（名前マッチング不使用）。

    _hide_top_level_except が返した「非表示にした index 配列」をそのまま渡す。
    名前で探さないため、末尾スペース・全角記号を含む層名でも確実に復元できる
    （SUKIMA タグライン層がサムネから消えるバグの恒久対策）。
    """
    idxs = [int(i) for i in indices if i is not None]
    if not idxs:
        return True
    idx_js = json.dumps(idxs)
    code = f"""
        if (!app.documents.length) {{ "ERROR: no active doc"; }}
        else (function() {{
            var idxs = {idx_js};
            var ls = app.activeDocument.layers;
            for (var i = 0; i < idxs.length; i++) {{
                var k = idxs[i];
                if (k >= 0 && k < ls.length) ls[k].visible = true;
            }}
            return "ok";
        }})();
    """
    result = run_jsx(code, timeout=20)
    if isinstance(result, str) and result.startswith("ERROR:"):
        raise RuntimeError(result)
    return result == "ok"


def render_thumbnail_set(
    psd_path: str,
    base_image: Optional[str] = None,
    base_layer: str = "base",
    text_replacements: Optional[dict] = None,
    toggle_layer: str = "PLAY LIST",
    out_dir: Optional[str] = None,
    vol_name: Optional[str] = None,
    quality: int = 90,
) -> dict:
    """PSD テンプレートから 2 枚のサムネを書き出す。

    工程:
      1. PSD を開く
      2. base_image を base_layer（スマートオブジェクト）に差し替え
      3. text_replacements の各 {layer_name: text} で文字を差し替え
      4. toggle_layer を表示 → out_dir/{vol_name}.jpg
      5. toggle_layer を非表示 → out_dir/サムネイル.jpg

    チャンネル別レイヤー名例:
      WW: base_layer="Chicago_Willis", toggle_layer="WORKSPACE"
      orzz: base_layer="base",         toggle_layer="PLAY LIST"

    Returns:
        {"with_toggle": ".../vol12.jpg", "thumbnail": ".../サムネイル.jpg"}
    """
    psd = Path(psd_path).expanduser().resolve()
    if not psd.exists():
        raise FileNotFoundError(f"PSD が存在しません: {psd}")
    out_dir_p = Path(out_dir).expanduser().resolve() if out_dir else psd.parent

    # vol_name を PSD ファイル名から推定（例: WW_vol12.psd → vol12）
    if not vol_name:
        m = re.search(r"vol\d+", psd.stem, re.IGNORECASE)
        vol_name = m.group(0).lower() if m else psd.stem

    print(f" open: {psd.name}")
    open_psd(str(psd))

    if base_image:
        print(f" '{base_layer}' に差し替え: {Path(base_image).name}")
        replace_smart_object(base_layer, base_image)

    if text_replacements:
        for layer, text in text_replacements.items():
            print(f"✍️  text: {layer} = {text!r}")
            set_text(layer, text)

    out_with = out_dir_p / f"{vol_name}.jpg"
    out_without = out_dir_p / "サムネイル.jpg"

    # toggle_layer が PSD に無い（テンプレ未整備）でも vol{N}.jpg は出す（graceful）。
    toggle_ok = True
    try:
        print(f" show '{toggle_layer}' → {out_with.name}")
        set_layer_visible(toggle_layer, True)
    except RuntimeError as e:
        if "layer not found" in str(e):
            print(f"  ⚠ toggle_layer '{toggle_layer}' が PSD に無い → 切替なしで {out_with.name} のみ出力（サムネイル.jpg はスキップ）")
            toggle_ok = False
        else:
            raise
    export_image(str(out_with), "jpg", quality)

    if toggle_ok:
        print(f" hide '{toggle_layer}' → {out_without.name}")
        set_layer_visible(toggle_layer, False)
        export_image(str(out_without), "jpg", quality)
        return {"with_toggle": str(out_with), "thumbnail": str(out_without)}
    return {"with_toggle": str(out_with), "thumbnail": ""}


def _rename_active_layer(new_name: str) -> bool:
    """activeLayer の名前を強制上書き。Smart Object 差し替えで名前が変わる現象の補正用。"""
    name_js = json.dumps(new_name)
    code = f"""
        if (!app.documents.length) {{ "ERROR: no active doc"; }}
        else (function() {{
            var l = app.activeDocument.activeLayer;
            if (!l) return "ERROR: no active layer";
            l.name = {name_js};
            return "ok";
        }})();
    """
    result = run_jsx(code, timeout=10)
    if isinstance(result, str) and result.startswith("ERROR:"):
        raise RuntimeError(result)
    return result == "ok"


def render_dual_thumbnail(
    psd_path: str,
    base_image: str,
    scene_text: str,
    out_dir: Optional[str] = None,
    vol_name: Optional[str] = None,
    base_layer: str = "base",
    scene_text_layer: str = "都市名_テキスト",
    playlist_layer: str = "PLAY LIST ",
    quality: int = 90,
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    save_psd: bool = False,
    scene_text_font: Optional[str] = None,
    scene_text_ja: Optional[str] = None,
    scene_text_ja_layer: Optional[str] = None,
    scene_text_ja_font: Optional[str] = None,
    toggle_always_visible: bool = False,
    bg_base_only: bool = False,
    center_text: bool = True,
) -> dict:
    """Harbor Notes 仕様: 2層対立式の2枚出力（背景画像 + サムネ）。

    工程:
      1. PSD を開く
      2. base_image を base_layer（スマートオブジェクト）に差し替え
      3. scene_text_layer に scene_text（英語シーン名）を設定 + 水平センター配置
      3b. （任意）scene_text_ja_layer に scene_text_ja（日本語コピー）を設定
      4. 出力1: vol{N}.jpg     — シーン文字OFF / PLAY LIST ON   （Premiere 背景画像用）
      5. 出力2: サムネイル.jpg — シーン文字ON  / PLAY LIST OFF  （YouTube サムネ用）

    toggle_always_visible=True（competitor 風 headline 常時表示）:
      - playlist_layer（= headline 層。例 SUKIMA "Playlist"）を **両出力で常時 ON** にする。
      - 背景/サムネの差は scene_text / scene_text_ja のオン/オフだけになる。
      - 出力1: vol{N}.jpg     — シーン文字OFF / headline ON   （Premiere 背景画像用）
        出力2: サムネイル.jpg — シーン文字ON  / headline ON   （YouTube サムネ用）
      - False（既定）なら従来の「toggle 層を背景=ON / サムネ=OFF で切替」挙動と完全に同一
        （orzz / Harbor Notes の英語専用 toggle を壊さない）。

    Args:
        psd_path: テンプレ PSD のパス
        base_image: スマートオブジェクト差し替え画像（AI生成 vol{N}.png 等）
        scene_text: 英語シーン層に流し込むシーン名（例: "午後の余韻" / "MORNING LIGHT"）
        out_dir: 出力先（既定: PSD と同じフォルダ）
        vol_name: 出力ファイル名のプレフィックス（既定: PSD ファイル名から `vol\\d+` を抽出）
        base_layer: スマートオブジェクト層名
        scene_text_layer: シーン名（英語）を入れるテキスト層名
        playlist_layer: 表示/非表示で切り替えるテキスト層名
        quality: JPG 品質（1-100）
        scene_text_ja: 日本語キャッチコピー（competitor 風）。None/空なら日本語層は触らない。
        scene_text_ja_layer: 日本語コピーを入れるテキスト層名。None なら日本語層は触らない。
        scene_text_ja_font: 日本語フォント名（例: "ヒラギノ角ゴシック W6"）。

    競合・分離の方針:
        scene_text_ja / scene_text_ja_layer が両方与えられたときだけ日本語層を操作する。
        与えられなければ従来の英語専用 2 枚出しと完全に同一の挙動になる。
        英語層（scene_text_layer）と日本語層（scene_text_ja_layer）が **同一名** の場合は
        日本語で英語を上書きしてしまうため、日本語側を優先しつつ警告を出す。

    Returns:
        {"bg": ".../vol{N}.jpg", "thumbnail": ".../サムネイル.jpg"}
    """
    use_ja = bool(scene_text_ja and scene_text_ja_layer)
    if use_ja and scene_text_ja_layer == scene_text_layer:
        print(f"⚠ 日本語層 '{scene_text_ja_layer}' が英語層と同名です。日本語コピーで上書きします（英語層は別名にしてください）")
    psd = Path(psd_path).expanduser().resolve()
    if not psd.exists():
        raise FileNotFoundError(f"PSD が存在しません: {psd}")
    out_dir_p = Path(out_dir).expanduser().resolve() if out_dir else psd.parent

    if not vol_name:
        m = re.search(r"vol\d+", psd.stem, re.IGNORECASE)
        vol_name = m.group(0).lower() if m else psd.stem

    print(f" open: {psd.name}")
    open_psd(str(psd))

    print(f" '{base_layer}' に差し替え: {Path(base_image).name}")
    replace_smart_object(base_layer, base_image)
    # Photoshop は SO 差し替え時にレイヤー名を画像ファイル名に変える → 元名に戻す
    try:
        _rename_active_layer(base_layer)
    except Exception as e:
        print(f"⚠ レイヤー名 '{base_layer}' 復元失敗（次回実行に影響なし）: {e}")

    print(f"✍️  text: {scene_text_layer} = {scene_text!r} (centered={center_text}{', font=' + scene_text_font if scene_text_font else ''})")
    set_text(scene_text_layer, scene_text, centered=center_text, font=scene_text_font)

    # 日本語コピー（competitor 風）— enabled かつ層が与えられたときだけ操作。
    # 失敗（層が見つからない/テキスト層でない等）は non-fatal で続行する
    # （英語サムネ出力は成功させ、日本語が乗らなかったことだけ警告）。
    if use_ja:
        print(f"✍️  text(ja): {scene_text_ja_layer} = {scene_text_ja!r} (centered={center_text}{', font=' + scene_text_ja_font if scene_text_ja_font else ''})")
        try:
            set_text(scene_text_ja_layer, scene_text_ja, centered=center_text, font=scene_text_ja_font)
        except Exception as e:
            print(f"⚠ 日本語層 '{scene_text_ja_layer}' へのテキスト設定に失敗（英語のみで続行）: {e}")
            use_ja = False

    out_bg = out_dir_p / f"{vol_name}.jpg"
    out_thumb = out_dir_p / "サムネイル.jpg"

    ja_off_note = f" / hide '{scene_text_ja_layer}'" if use_ja else ""
    ja_on_note = f" / show '{scene_text_ja_layer}'" if use_ja else ""

    # toggle_always_visible: competitor 風に headline（playlist_layer）を両出力で常時表示。
    #   両出力の差は scene_text / scene_text_ja のオン/オフだけになる。
    # 既定（False）: 従来どおり背景=playlist ON / サムネ=playlist OFF で切り替える。
    if toggle_always_visible:
        print(f" headline 常時表示モード: '{playlist_layer}' は両出力で ON")
        bg_toggle_note = on_toggle_note = f" / show '{playlist_layer}' (always)"
    else:
        bg_toggle_note = f" / show '{playlist_layer}'"
        on_toggle_note = f" / hide '{playlist_layer}'"

    # 出力1: vol{N}.jpg（Premiere 背景用）
    if bg_base_only:
        # 背景は base レイヤーのみ（テキスト/ロゴ層を全部 OFF）。SUKIMA 等
        # 「背景に文字を焼き込まない」運用向け。出力後に元の可視状態へ復元する。
        print(f" 背景モード: '{base_layer}' 以外の全トップ層を非表示 → {out_bg.name}")
        hidden_idx = _hide_top_level_except(base_layer)
        export_image(str(out_bg), "jpg", quality, target_width=target_width, target_height=target_height)
        # 復元は index で行う（名前マッチングだと SUKIMA のタグライン層
        # 'cozy tune / morning cafe / bossa ＆ soul '（末尾スペース・全角＆）が
        # 復元されず、サムネからタグラインが消える事故が起きるため）。
        try:
            _show_top_level_by_index(hidden_idx)
        except Exception as e:
            print(f"⚠ 背景モードのトップ層復元に失敗（続行）: {e}")
    else:
        # 従来: シーン文字OFF / headline（toggle）ON
        print(f" hide '{scene_text_layer}'{ja_off_note}{bg_toggle_note} → {out_bg.name}")
        set_layer_visible(scene_text_layer, False)
        if use_ja:
            try:
                set_layer_visible(scene_text_ja_layer, False)
            except Exception as e:
                print(f"⚠ 日本語層 '{scene_text_ja_layer}' の非表示化に失敗（続行）: {e}")
        try:
            set_layer_visible(playlist_layer, True)
        except RuntimeError as e:
            if "layer not found" in str(e):
                print(f"  ⚠ playlist_layer '{playlist_layer}' が PSD に無い → スキップ（{out_bg.name} は playlist 無しで出力）")
            else:
                raise
        export_image(str(out_bg), "jpg", quality, target_width=target_width, target_height=target_height)

    # 出力2: サムネイル.jpg — シーン文字ON / headline は常時表示なら ON、従来は OFF
    print(f" show '{scene_text_layer}'{ja_on_note}{on_toggle_note} → {out_thumb.name}")
    set_layer_visible(scene_text_layer, True)
    if use_ja:
        try:
            set_layer_visible(scene_text_ja_layer, True)
        except Exception as e:
            print(f"⚠ 日本語層 '{scene_text_ja_layer}' の表示化に失敗（続行）: {e}")
    # 常時表示モードなら headline は ON のまま、従来モードなら OFF にする。
    try:
        set_layer_visible(playlist_layer, True if toggle_always_visible else False)
    except RuntimeError as e:
        if "layer not found" not in str(e):
            raise
    export_image(str(out_thumb), "jpg", quality, target_width=target_width, target_height=target_height)

    if save_psd:
        # PSD 自体を save（vol 固有 PSD として編集状態を残す。Photoshop の dirty フラグも下がる）
        save_code = """
            if (!app.documents.length) { "ERROR: no active doc"; }
            else (function() {
                app.activeDocument.save();
                "ok";
            })();
        """
        result = run_jsx(save_code, timeout=240)
        if isinstance(result, str) and result.startswith("ERROR:"):
            print(f"⚠ PSD save 失敗（書き出しは成功・編集状態は未保存）: {result}")
        else:
            print(f" PSD save: {psd.name}")
        # 書き出し + save が完了したので document を close（Photoshop アプリは起動したまま）。
        # 連続 vol 処理時にドキュメントが溜まるのを防ぐ。save_psd=False の場合は呼び出し側で
        # 未保存変更があるはずなので close しない（誤って失う可能性）。
        close_code = """
            if (!app.documents.length) { "ERROR: no active doc"; }
            else (function() {
                app.activeDocument.close(SaveOptions.DONOTSAVECHANGES);
                "ok";
            })();
        """
        cresult = run_jsx(close_code, timeout=30)
        if isinstance(cresult, str) and cresult.startswith("ERROR:"):
            print(f"⚠ PSD close 失敗（非致命、Photoshop は起動継続）: {cresult}")
        else:
            print(f" PSD close: {psd.name}")

    return {"bg": str(out_bg), "thumbnail": str(out_thumb)}


def _find_video_psd(video_folder: Path) -> Path:
    """動画フォルダから PSD を 1 つ選ぶ（*vol*.psd 優先、無ければ最初の .psd）。"""
    candidates = sorted(video_folder.glob("*vol*.psd")) or sorted(video_folder.glob("*.psd"))
    if not candidates:
        raise FileNotFoundError(f"PSD が見つかりません: {video_folder}")
    return candidates[0]


def _find_swap_image(video_folder: Path, image_subdir: str) -> Optional[Path]:
    """動画フォルダ内の image_subdir から差し替え用画像を 1 つ選ぶ。

    大文字小文字違いのフォルダ名（image / Image / IMAGE）にも対応。
    PNG > JPG > JPEG > WebP の優先順で探索。
    """
    base = video_folder / image_subdir
    if not base.exists():
        # 大文字小文字違いを探す
        for d in video_folder.iterdir() if video_folder.exists() else []:
            if d.is_dir() and d.name.lower() == image_subdir.lower():
                base = d
                break
        else:
            return None
    if not base.exists() or not base.is_dir():
        return None
    for ext in ("png", "jpg", "jpeg", "webp"):
        for f in sorted(base.glob(f"*.{ext}")):
            return f
        for f in sorted(base.glob(f"*.{ext.upper()}")):
            return f
    return None


def render_thumbnails_for_video(
    video_folder: str,
    *,
    base_layer: str = "base",
    toggle_layer: str = "PLAY LIST",
    image_subdir: str = "image",
    text_replacements: Optional[dict] = None,
    quality: int = 90,
) -> dict:
    """動画フォルダの PSD と画像を自動検出してサムネを書き出す。

    検出ルール:
        PSD:   <video_folder>/*vol*.psd（最初の 1 件）
        画像:  <video_folder>/{image_subdir}/<最初の画像>

    Returns:
        {
            "psd": "...",
            "swap_image": "...",
            "with_toggle": "..../vol12.jpg",
            "thumbnail": "..../サムネイル.jpg",
        }
    """
    folder = Path(video_folder).expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"動画フォルダが存在しません: {folder}")

    psd = _find_video_psd(folder)
    swap = _find_swap_image(folder, image_subdir)
    if swap is None:
        print(f"⚠️  {image_subdir}/ に画像が見つかりません。base 差し替えはスキップ")

    result = render_thumbnail_set(
        str(psd),
        base_image=str(swap) if swap else None,
        base_layer=base_layer,
        text_replacements=text_replacements,
        toggle_layer=toggle_layer,
        out_dir=str(folder),
        quality=quality,
    )
    result["psd"] = str(psd)
    result["swap_image"] = str(swap) if swap else None
    return result


# ─── CLI（手動デバッグ用）────────────────────────────────────────────────────

def _cli():
    p = argparse.ArgumentParser(description="Photoshop Link CEP CLI")
    p.add_argument("--check", action="store_true", help="接続確認")
    p.add_argument("--open", metavar="PSD", help="PSD を開く")
    p.add_argument("--layers", action="store_true", help="アクティブ doc のレイヤー一覧")
    p.add_argument("--export", metavar="OUT", help="アクティブ doc を画像として書き出し")
    p.add_argument("--fmt", default="jpg", help="export 形式 jpg|png")
    p.add_argument("--quality", type=int, default=90, help="JPG 品質")
    p.add_argument("--set-text", nargs=2, metavar=("LAYER", "TEXT"), help="テキストレイヤーを差し替え")
    p.add_argument("--replace-so", nargs=2, metavar=("LAYER", "IMAGE"), help="スマートオブジェクトの中身を画像で差し替え")
    p.add_argument("--set-visible", nargs=2, metavar=("LAYER", "BOOL"), help="レイヤーの表示状態を変更 (true/false)")
    p.add_argument("--render-thumbs", metavar="PSD", help="サムネ 2 枚セットを書き出し（要 --base-image または --set-text）")
    p.add_argument("--render-for-video", metavar="VIDEO_FOLDER", help="動画フォルダから PSD と画像を自動検出してサムネ 2 枚出力")
    p.add_argument("--base-image", metavar="IMAGE", help="render-thumbs で base レイヤーに入れる画像")
    p.add_argument("--base-layer", default="base", help="差し替え対象の SO レイヤー名（WW なら Chicago_Willis）")
    p.add_argument("--text", action="append", metavar="LAYER=TEXT", default=[], help="render-thumbs でテキスト差し替え（複数可）")
    p.add_argument("--toggle-layer", default="PLAY LIST", help="表示/非表示で切り替えるレイヤー名（WW なら WORKSPACE）")
    p.add_argument("--image-subdir", default="image", help="render-for-video が探す差し替え画像の置き場サブディレクトリ")
    p.add_argument("--vol", help="render-thumbs で出力するファイル名の vol 部分（例: vol12）。未指定なら PSD 名から推定")
    p.add_argument("--out-dir", help="render-thumbs の出力先（未指定なら PSD と同じフォルダ）")
    p.add_argument("--eval", metavar="JSCODE", help="任意の ExtendScript を実行")
    args = p.parse_args()

    if args.check:
        print(json.dumps(check_photoshop(), ensure_ascii=False, indent=2))
        return

    if args.open:
        print(" open:", args.open)
        print("→", open_psd(args.open))

    if args.layers:
        layers = list_layers()
        print(f" {len(layers)} layers:")
        for l in layers:
            print(f"  [{l.get('kind','?'):>20}] {l.get('path','')}")

    if args.set_text:
        layer, text = args.set_text
        print(f"✍️  set text: {layer} = {text!r}")
        print("→", set_text(layer, text))

    if args.replace_so:
        layer, img = args.replace_so
        print(f" replace SO: {layer} ← {img}")
        print("→", replace_smart_object(layer, img))

    if args.set_visible:
        layer, val = args.set_visible
        vis = val.lower() in ("true", "1", "yes", "on")
        print(f" set visible: {layer} = {vis}")
        print("→", set_layer_visible(layer, vis))

    if args.export:
        print(f" export ({args.fmt}, q={args.quality}): {args.export}")
        print("→", export_image(args.export, args.fmt, args.quality))

    if args.render_thumbs:
        text_repl = {}
        for kv in args.text:
            if "=" not in kv:
                raise ValueError(f"--text の書式が不正: {kv} (LAYER=TEXT)")
            k, v = kv.split("=", 1)
            text_repl[k.strip()] = v
        result = render_thumbnail_set(
            args.render_thumbs,
            base_image=args.base_image,
            base_layer=args.base_layer,
            text_replacements=text_repl or None,
            toggle_layer=args.toggle_layer,
            out_dir=args.out_dir,
            vol_name=args.vol,
            quality=args.quality,
        )
        print()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.render_for_video:
        text_repl = {}
        for kv in args.text:
            if "=" not in kv:
                raise ValueError(f"--text の書式が不正: {kv} (LAYER=TEXT)")
            k, v = kv.split("=", 1)
            text_repl[k.strip()] = v
        result = render_thumbnails_for_video(
            args.render_for_video,
            base_layer=args.base_layer,
            toggle_layer=args.toggle_layer,
            image_subdir=args.image_subdir,
            text_replacements=text_repl or None,
            quality=args.quality,
        )
        print()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.eval:
        print(json.dumps(run_jsx(args.eval), ensure_ascii=False, indent=2, default=str))

    if not any([args.check, args.open, args.layers, args.export, args.set_text,
                args.replace_so, args.set_visible, args.render_thumbs,
                args.render_for_video, args.eval]):
        p.print_help()


if __name__ == "__main__":
    try:
        _cli()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
