"""共通: 設定ディレクトリの解決と旧→新マイグレーション。

優先順位（resolve_config_dir）:
  1. 環境変数 APP_CONFIG_DIR があればそれを尊重
  2. 旧 ~/.config/orzz/dashboard_config.json から app_id を読み、~/.config/{app_id}/
  3. 既定 ~/.config/orzz/

migrate_legacy_if_needed:
  - 旧 ~/.config/orzz/ → 新 ~/.config/{app_id}/ へ未存在ファイルだけコピー
  - 旧ディレクトリは削除しない（ロールバック可能）
  - ~/.config/{app_id}/migration.log に履歴を追記
  - 戻り値: {"performed": bool, "src": str, "dst": str, "copied": [...], "skipped": [...]}
"""
from __future__ import annotations

import json
import os
import shutil
import datetime
from pathlib import Path
from typing import Optional

HOME = Path.home()
LEGACY_APP_ID = "orzz"
LEGACY_CONFIG_DIR = HOME / ".config" / LEGACY_APP_ID
DASHBOARD_FILE = "dashboard_config.json"
MIGRATION_LOG = "migration.log"
MIGRATION_FLAG = ".migration_completed"
# チャンネル切替が書く「アクティブ app_id ポインタ」。次回 resolve_app_id がこれを最優先で採用する。
# （channels.json の per-ch app_id をチャンネル切替時に書き込み、再起動で profile を遷移させるための単一情報源）
ACTIVE_APP_ID_POINTER = HOME / ".config" / "active_app_id"


def _read_active_app_id_pointer() -> Optional[str]:
    try:
        v = ACTIVE_APP_ID_POINTER.read_text(encoding="utf-8").strip()
        return v or None
    except Exception:
        return None


def set_active_app_id(app_id: str) -> None:
    """チャンネル切替時に呼ぶ。次回起動の resolve_app_id がこの app_id を最優先採用する。"""
    try:
        ACTIVE_APP_ID_POINTER.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE_APP_ID_POINTER.write_text((app_id or "").strip() + "\n", encoding="utf-8")
    except Exception:
        pass


def _read_app_id_from(legacy_dir: Path) -> Optional[str]:
    """旧 dashboard_config.json から app_id を読む（存在しなければ None）"""
    p = legacy_dir / DASHBOARD_FILE
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        v = d.get("app_id")
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:
        pass
    return None


def _scan_active_app_id() -> Optional[str]:
    """~/.config/ 配下で dashboard_config.json を持つディレクトリを探す。
    複数あれば dashboard_config.json の mtime が最新のものを採用。
    レガシー orzz dir を消しても app_id を解決できるようにするための保険。"""
    base = HOME / ".config"
    if not base.is_dir():
        return None
    candidates = []
    for d in base.iterdir():
        try:
            if not d.is_dir():
                continue
            f = d / DASHBOARD_FILE
            if f.exists():
                candidates.append((f.stat().st_mtime, d.name))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(reverse=True)  # mtime 最新を優先
    return candidates[0][1]


def resolve_app_id() -> str:
    """app_id を解決。
    優先順位: APP_ID 環境変数 > レガシー orzz/dashboard の app_id(指す先が実在する場合) >
    ~/.config 配下のアクティブな設定dir(mtime最新) > "orzz"。
    D4: レガシー orzz dashboard への隠れ依存を断ち、orzz dir 削除でも壊れないようにした。"""
    env = os.environ.get("APP_ID")
    if env and env.strip():
        return env.strip()
    # チャンネル切替が書くアクティブ app_id ポインタ（最優先・指す先が実在する場合のみ）。
    # これにより「チャンネル切替 → app_id 遷移」が決定論的に効く（profile/benchmark/suno が ch に追従）。
    ptr = _read_active_app_id_pointer()
    if ptr and (HOME / ".config" / ptr).is_dir():
        return ptr
    # レガシー orzz/dashboard_config.json の app_id（後方互換）。
    # ただし指す先 ~/.config/{app_id}/ が実在する場合のみ信用する（隠れ依存の検証）。
    from_file = _read_app_id_from(LEGACY_CONFIG_DIR)
    if from_file and (HOME / ".config" / from_file).is_dir():
        return from_file
    # アクティブな設定dirをスキャン（orzz dir を消しても解決できる保険）。
    active = _scan_active_app_id()
    if active:
        return active
    return LEGACY_APP_ID


def resolve_config_dir() -> Path:
    """設定ディレクトリを解決。
    APP_CONFIG_DIR > ~/.config/{app_id}/ の順。"""
    env = os.environ.get("APP_CONFIG_DIR")
    if env and env.strip():
        return Path(env).expanduser()
    app_id = resolve_app_id()
    return HOME / ".config" / app_id


def resolve_shared_base() -> Path:
    """共有ドライブ上のアプリベース（<共有ドライブ>/DEV/_claude）を解決する。

    優先順位: 環境変数 APP_SHARED_BASE / ORZZ_SHARED_BASE → このモジュール自身の位置。
    本モジュール（およびアプリ一式）は共有ドライブ上の <SHARED>/Python/ 配下に置かれる
    前提なので、__file__ の親の親が共有ベースになる（最も確実）。"""
    env_path = os.environ.get("APP_SHARED_BASE") or os.environ.get("ORZZ_SHARED_BASE")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p
    return Path(__file__).resolve().parent.parent


def resolve_shared_config_dir() -> Path:
    """PC 間で共有する設定/データの置き場（<共有ドライブ>/DEV/_claude/config）。
    channels.json・ベンチマークのプロファイル/設定/分析などを置く。

    launchd 常駐時はコードをローカルミラーから起動するが、共有 config は
    Drive 側を真実にする必要があるため、STUDIO_CONFIG_DIR で明示できる。
    """
    env_path = (
        os.environ.get("STUDIO_CONFIG_DIR")
        or os.environ.get("APP_SHARED_CONFIG_DIR")
        or os.environ.get("ORZZ_SHARED_CONFIG_DIR")
    )
    if env_path:
        return Path(env_path).expanduser()
    return resolve_shared_base() / "config"


def _is_safe_legacy_subpath(p: Path, base: Path) -> bool:
    """シンボリックリンクや base 外への脱出を防ぐ"""
    try:
        rp = p.resolve(strict=True)
        return str(rp).startswith(str(base.resolve(strict=False)))
    except Exception:
        return False


def migrate_legacy_if_needed(verbose: bool = False) -> dict:
    """旧 ~/.config/orzz/ → 新 ~/.config/{app_id}/ への自動マイグレーション。

    既に同名ファイルが新側にあれば上書きしない。旧ディレクトリは削除しない。
    """
    src = LEGACY_CONFIG_DIR
    dst = resolve_config_dir()
    result = {
        "performed": False,
        "src": str(src),
        "dst": str(dst),
        "copied": [],
        "skipped": [],
        "skipped_reason": "",
        "errors": [],
        "migrated_at": None,
    }
    # 旧と新が同じ → 何もしない
    if src.resolve(strict=False) == dst.resolve(strict=False):
        result["skipped_reason"] = "same_dir"
        return result
    # 旧が無い → 何もしない（フレッシュインストール想定）
    if not src.exists() or not src.is_dir():
        result["skipped_reason"] = "no_legacy"
        return result
    # 既に flag があればスキップ（毎回コピーは無駄）
    flag = dst / MIGRATION_FLAG
    if flag.exists():
        result["skipped_reason"] = "already_migrated"
        return result

    dst.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.datetime.now().isoformat(timespec="seconds")

    # 再帰コピー: 旧ディレクトリ配下のファイル / サブディレクトリを順次コピー
    # 再構築可能 / 一時的なディレクトリは除外（venv / __pycache__ / node_modules /
    # Playwright プロファイル等）— 容量が肥大化しがちなものを排除
    EXCLUDE_NAMES = {".DS_Store"}
    EXCLUDE_DIRS = {
        "venv", ".venv", "__pycache__", "node_modules", ".pytest_cache",
        ".cache", ".git",
        # Playwright / Chromium プロファイル（289MB+ になる、再ログイン推奨）
        "chromium_profile", ".flow-playwright-profile",
        # ブラウザキャッシュ系
        "Cache", "GPUCache", "ShaderCache", "GrShaderCache", "GraphiteDawnCache",
        "Code Cache", "Service Worker",
    }
    for entry in src.rglob("*"):
        try:
            rel = entry.relative_to(src)
        except Exception:
            continue
        if entry.name in EXCLUDE_NAMES:
            continue
        # パス成分のいずれかが除外ディレクトリ名に一致したらスキップ
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        target = dst / rel
        if entry.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if entry.is_symlink():
            # symlink はコピーしない（解決先が脱出する可能性）
            result["skipped"].append({"path": str(rel), "reason": "symlink"})
            continue
        if target.exists():
            result["skipped"].append({"path": str(rel), "reason": "already_exists"})
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, target)
            # 機密ファイルは 600 に
            sensitive = entry.name in {
                "youtube_client_secret.json", "youtube_token.json",
                "discord_config.json", "line_config.json", "suno_config.json",
                "auth_token.txt",
            }
            if sensitive:
                try:
                    target.chmod(0o600)
                except Exception:
                    pass
            result["copied"].append(str(rel))
        except Exception as e:
            result["errors"].append({"path": str(rel), "error": str(e)})

    result["performed"] = True
    result["migrated_at"] = now_iso

    # migration.log 追記
    try:
        log_path = dst / MIGRATION_LOG
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "migrated_at": now_iso,
                "src": str(src),
                "dst": str(dst),
                "copied_count": len(result["copied"]),
                "skipped_count": len(result["skipped"]),
                "error_count": len(result["errors"]),
                "copied_sample": result["copied"][:10],
                "errors": result["errors"][:5],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # 完了フラグ（次回起動でスキップさせる）
    try:
        flag.write_text(now_iso, encoding="utf-8")
    except Exception:
        pass

    if verbose:
        print(f"[migrate] {len(result['copied'])} files copied {src} → {dst}")

    return result


# ─── 環境変数の互換読み（ORZZ_* も同義として尊重） ───
def env(name: str, default: Optional[str] = None) -> Optional[str]:
    """環境変数を APP_* 優先・ORZZ_* フォールバックで取得。

    例: env("PIPELINE_STEPS") は APP_PIPELINE_STEPS → ORZZ_PIPELINE_STEPS の順。
    name は接頭辞なし（"PIPELINE_STEPS" 等）で渡す。

    互換用: 完全一致（"ORZZ_X"/"APP_X" を含む name）にも対応。
    """
    if name.startswith("APP_") or name.startswith("ORZZ_"):
        # 完全名で渡された場合は対応する反対側もチェック
        v = os.environ.get(name)
        if v is not None:
            return v
        if name.startswith("APP_"):
            return os.environ.get("ORZZ_" + name[4:], default)
        else:
            return os.environ.get("APP_" + name[5:], default)
    # 接頭辞なし: APP_X を優先、ORZZ_X をフォールバック
    v = os.environ.get("APP_" + name)
    if v is not None:
        return v
    return os.environ.get("ORZZ_" + name, default)


# モジュールロード時には実行しない。app.py が起動時に明示的に呼ぶ。
