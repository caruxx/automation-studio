#!/usr/bin/env python3
"""Import channels.json into the VPS channel table without modifying the source."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Iterable

from sqlalchemy.exc import SQLAlchemyError


VPS_ROOT = Path(__file__).resolve().parents[1]
if str(VPS_ROOT) not in sys.path:
    sys.path.insert(0, str(VPS_ROOT))

from app.db import SessionLocal
from app.models.db_models import YouTubeChannel


SHARED_DRIVE_MARKER = "/共有ドライブ/"
UPDATE_FIELDS = ("name", "handle", "youtube_channel_id", "prefix", "folder_rel")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import channels.json into PostgreSQL")
    parser.add_argument("--source", required=True, type=Path, help="Path to channels.json")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    return parser.parse_args()


def _channel_entries(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict) and isinstance(payload.get("channels"), list):
        entries = payload["channels"]
    elif isinstance(payload, dict) and isinstance(payload.get("channels"), dict):
        entries = []
        for channel_key, value in payload["channels"].items():
            if isinstance(value, dict):
                entries.append({"id": channel_key, **value})
    elif isinstance(payload, dict):
        entries = []
        for channel_key, value in payload.items():
            if isinstance(value, dict):
                entries.append({"id": channel_key, **value})
    else:
        raise ValueError("channels.json must contain a channel list or object")

    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Every channel entry must be an object")
        yield entry


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _folder_rel(folder: Any) -> str | None:
    value = _optional_text(folder)
    if value is None or SHARED_DRIVE_MARKER not in value:
        return None
    relative = value.split(SHARED_DRIVE_MARKER, 1)[1].lstrip("/")
    return relative or None


def _import_values(entry: dict[str, Any]) -> tuple[str, dict[str, str | None]]:
    channel_key = _optional_text(entry.get("id"))
    if channel_key is None:
        raise ValueError("Every channel entry requires a non-empty id")
    name = _optional_text(entry.get("name"))
    if name is None:
        raise ValueError(f"Channel {channel_key} requires a non-empty name")
    youtube = entry.get("youtube") if isinstance(entry.get("youtube"), dict) else {}
    youtube_channel_id = entry.get("youtube_channel_id")
    if youtube_channel_id is None:
        youtube_channel_id = entry.get("channel_id", youtube.get("channel_id"))
    return channel_key, {
        "name": name,
        "handle": _optional_text(entry.get("handle")),
        "youtube_channel_id": _optional_text(youtube_channel_id),
        "prefix": _optional_text(entry.get("prefix")),
        "folder_rel": _folder_rel(entry.get("folder")),
    }


def main() -> int:
    args = parse_args()
    try:
        payload = json.loads(args.source.read_text(encoding="utf-8"))
        entries = list(_channel_entries(payload))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1

    created = 0
    updated = 0
    try:
        with SessionLocal() as db:
            for entry in entries:
                channel_key, values = _import_values(entry)
                channel = (
                    db.query(YouTubeChannel)
                    .filter(YouTubeChannel.channel_key == channel_key)
                    .first()
                )
                if channel is None:
                    created += 1
                    action = "create"
                    if not args.dry_run:
                        db.add(YouTubeChannel(channel_key=channel_key, **values))
                else:
                    updated += 1
                    action = "update"
                    if not args.dry_run:
                        for field_name in UPDATE_FIELDS:
                            setattr(channel, field_name, values[field_name])
                print(f"{action}: {channel_key}")
            if not args.dry_run:
                db.commit()
    except (ValueError, SQLAlchemyError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1

    mode = "dry-run" if args.dry_run else "committed"
    print(f"{mode}: {created} create, {updated} update")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
