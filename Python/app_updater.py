#!/usr/bin/env python3
"""Automation Studio updater.

コード領域だけを更新し、config/・チャンネルデータ・トークン類は触らない。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from urllib.parse import urlparse
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT_DIR / "config"
VERSION_FILE = ROOT_DIR / "VERSION"
BACKUP_DIR = ROOT_DIR / "backup"

CODE_PATHS = [
    "Python",
    "web",
    "skills",
    "routes.json",
    "start.sh",
    "VERSION",
]

UPDATE_TEMPLATE = {
    "method": "zip_url",
    "source": "",
    "check_interval_hours": 24,
    "auto_apply": True,
}


@dataclass(frozen=True)
class UpdateCandidate:
    configured: bool
    method: str
    source: str
    current_version: str
    latest_version: str
    update_available: bool
    message: str
    manifest: dict[str, Any] | None = None


def _json(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def read_version(root: Path = ROOT_DIR) -> str:
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except FileNotFoundError:
        return "0.0.0"


def semver_tuple(v: str) -> tuple[int, int, int, tuple[Any, ...]]:
    """Small semantic-version parser supporting x.y.z[-pre]."""
    core, _, pre = (v or "0.0.0").strip().lstrip("v").partition("-")
    nums = []
    for part in core.split(".")[:3]:
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    pre_key: tuple[Any, ...] = (1,) if not pre else (0, tuple(pre.split(".")))
    return nums[0], nums[1], nums[2], pre_key


def is_newer(latest: str, current: str) -> bool:
    return semver_tuple(latest) > semver_tuple(current)


def load_update_config(root: Path = ROOT_DIR) -> dict[str, Any]:
    path = root / "config" / "update_config.json"
    template = root / "config" / "update_config.template.json"
    data: dict[str, Any] = {}
    for p in (path, template):
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                break
            except Exception:
                data = {}
    cfg = {**UPDATE_TEMPLATE, **(data if isinstance(data, dict) else {})}
    method = str(cfg.get("method") or "zip_url").strip().lower()
    if method not in {"zip_url", "git"}:
        method = "zip_url"
    cfg["method"] = method
    cfg["source"] = str(cfg.get("source") or "").strip()
    try:
        cfg["check_interval_hours"] = int(cfg.get("check_interval_hours") or 24)
    except Exception:
        cfg["check_interval_hours"] = 24
    cfg["auto_apply"] = bool(cfg.get("auto_apply", True))
    return cfg


def fetch_json(url: str, timeout: int = 20) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme in ("", "file"):
        path = Path(urllib.request.url2pathname(parsed.path if parsed.scheme == "file" else url)).expanduser()
        return json.loads(path.read_text(encoding="utf-8"))
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def check_zip_url(source: str, current: str) -> UpdateCandidate:
    manifest = fetch_json(source)
    latest = str(manifest.get("version") or "").strip()
    zip_url = str(manifest.get("zip_url") or "").strip()
    if not latest or not zip_url:
        return UpdateCandidate(True, "zip_url", source, current, "", False, "manifest に version / zip_url がありません", manifest)
    newer = is_newer(latest, current)
    msg = f"更新あり: {current} -> {latest}" if newer else f"最新です: {current}"
    return UpdateCandidate(True, "zip_url", source, current, latest, newer, msg, manifest)


def _semver_tags_from_ls_remote(raw: str) -> list[str]:
    versions: list[str] = []
    for line in raw.splitlines():
        ref = line.split()[-1] if line.split() else ""
        name = ref.rsplit("/", 1)[-1].replace("^{}", "")
        if name.startswith("v"):
            name = name[1:]
        if name and name[0].isdigit():
            versions.append(name)
    return sorted(set(versions), key=semver_tuple)


def check_git(source: str, current: str) -> UpdateCandidate:
    try:
        raw = subprocess.check_output(
            ["git", "ls-remote", "--tags", source],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        versions = _semver_tags_from_ls_remote(raw)
    except Exception as e:
        return UpdateCandidate(True, "git", source, current, "", False, f"git 更新確認に失敗: {e}")
    latest = versions[-1] if versions else ""
    if not latest:
        return UpdateCandidate(True, "git", source, current, "", False, "git tag からバージョンを確認できませんでした")
    newer = is_newer(latest, current)
    msg = f"更新あり: {current} -> {latest}" if newer else f"最新です: {current}"
    return UpdateCandidate(True, "git", source, current, latest, newer, msg)


def check_for_update(root: Path = ROOT_DIR) -> dict[str, Any]:
    cfg = load_update_config(root)
    current = read_version(root)
    if not cfg["source"]:
        return {
            "ok": True,
            "configured": False,
            "method": cfg["method"],
            "source": "",
            "current_version": current,
            "latest_version": "",
            "update_available": False,
            "message": "更新元は未設定です",
            "check_interval_hours": cfg["check_interval_hours"],
            "auto_apply": cfg["auto_apply"],
        }
    try:
        cand = check_zip_url(cfg["source"], current) if cfg["method"] == "zip_url" else check_git(cfg["source"], current)
        return {
            "ok": True,
            "configured": cand.configured,
            "method": cand.method,
            "source": cand.source,
            "current_version": cand.current_version,
            "latest_version": cand.latest_version,
            "update_available": cand.update_available,
            "message": cand.message,
            "check_interval_hours": cfg["check_interval_hours"],
            "auto_apply": cfg["auto_apply"],
            "manifest": cand.manifest or {},
        }
    except Exception as e:
        return {
            "ok": False,
            "configured": True,
            "method": cfg["method"],
            "source": cfg["source"],
            "current_version": current,
            "latest_version": "",
            "update_available": False,
            "message": f"更新確認に失敗: {e}",
            "check_interval_hours": cfg["check_interval_hours"],
            "auto_apply": cfg["auto_apply"],
        }


def _package_root(extracted: Path) -> Path:
    direct = extracted / "automation-studio"
    if direct.exists():
        return direct
    children = [p for p in extracted.iterdir() if p.is_dir()]
    if len(children) == 1 and (children[0] / "VERSION").exists():
        return children[0]
    return extracted


def _backup_target(version: str, root: Path) -> Path:
    base = root / "backup" / version
    if not base.exists():
        return base
    idx = 1
    while True:
        cand = root / "backup" / f"{version}-{idx}"
        if not cand.exists():
            return cand
        idx += 1


def backup_code(root: Path = ROOT_DIR) -> Path:
    current = read_version(root)
    backup = _backup_target(current, root)
    backup.mkdir(parents=True, exist_ok=True)
    for rel in CODE_PATHS:
        src = root / rel
        if not src.exists():
            continue
        dst = backup / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
    return backup


def apply_from_dir(src_root: Path, root: Path = ROOT_DIR) -> dict[str, Any]:
    if not (src_root / "VERSION").exists():
        raise RuntimeError("更新パッケージに VERSION がありません")
    current = read_version(root)
    incoming = read_version(src_root)
    backup = backup_code(root)
    copied: list[str] = []
    for rel in CODE_PATHS:
        src = src_root / rel
        if not src.exists():
            continue
        dst = root / rel
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        else:
            shutil.copy2(src, dst)
        copied.append(rel)
    return {
        "ok": True,
        "current_version": current,
        "new_version": incoming,
        "backup": str(backup),
        "copied": copied,
        "message": "更新を適用しました。bash Python/start.sh で再起動してください。",
    }


def apply_from_zip(zip_path: str | Path, root: Path = ROOT_DIR) -> dict[str, Any]:
    zpath = Path(zip_path).expanduser().resolve()
    if not zpath.exists():
        raise FileNotFoundError(str(zpath))
    with tempfile.TemporaryDirectory(prefix="automation-studio-update-") as td:
        tmp = Path(td)
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(tmp)
        return apply_from_dir(_package_root(tmp), root)


def download_and_apply(manifest: dict[str, Any], root: Path = ROOT_DIR) -> dict[str, Any]:
    url = str(manifest.get("zip_url") or "").strip()
    if not url:
        raise RuntimeError("manifest に zip_url がありません")
    with tempfile.TemporaryDirectory(prefix="automation-studio-download-") as td:
        zip_path = Path(td) / "update.zip"
        parsed = urlparse(url)
        if parsed.scheme in ("", "file"):
            src = Path(urllib.request.url2pathname(parsed.path if parsed.scheme == "file" else url)).expanduser()
            shutil.copy2(src, zip_path)
        else:
            with urllib.request.urlopen(url, timeout=120) as response, zip_path.open("wb") as out:
                shutil.copyfileobj(response, out)
        return apply_from_zip(zip_path, root)


def apply_git(source: str, root: Path = ROOT_DIR) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="automation-studio-git-") as td:
        tmp = Path(td) / "repo"
        subprocess.check_call(
            ["git", "clone", "--depth", "1", source, str(tmp)], timeout=300
        )
        return apply_from_dir(tmp, root)


def update(root: Path = ROOT_DIR) -> dict[str, Any]:
    check = check_for_update(root)
    if not check.get("ok"):
        return check
    if not check.get("configured"):
        return check
    if not check.get("update_available"):
        return check
    if check.get("method") == "zip_url":
        return download_and_apply(check.get("manifest") or {}, root)
    if check.get("method") == "git":
        return apply_git(str(check.get("source") or ""), root)
    return {"ok": False, "message": "未対応の更新方式です"}


def rollback(root: Path = ROOT_DIR) -> dict[str, Any]:
    backup_root = root / "backup"
    if not backup_root.exists():
        return {"ok": False, "message": "rollback 用 backup がありません"}
    candidates = [p for p in backup_root.iterdir() if p.is_dir()]
    if not candidates:
        return {"ok": False, "message": "rollback 用 backup がありません"}
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    current = read_version(root)
    for rel in CODE_PATHS:
        src = latest / rel
        dst = root / rel
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
    return {
        "ok": True,
        "current_version": current,
        "restored_version": read_version(latest),
        "backup": str(latest),
        "message": "ロールバックしました。bash Python/start.sh で再起動してください。",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Automation Studio updater")
    parser.add_argument("--check", action="store_true", help="更新確認のみ")
    parser.add_argument("--from-zip", help="ローカル zip から更新")
    parser.add_argument("--rollback", action="store_true", help="直近 backup から復元")
    args = parser.parse_args(argv)

    try:
        if args.rollback:
            _json(rollback())
        elif args.from_zip:
            _json(apply_from_zip(args.from_zip))
        elif args.check:
            _json(check_for_update())
        else:
            _json(update())
        return 0
    except Exception as e:
        _json({"ok": False, "message": str(e)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
