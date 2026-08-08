#!/usr/bin/env python3
from __future__ import annotations

import argparse
from base64 import b64encode
import json
import os
from pathlib import Path
import secrets
import sys


def _repository_root() -> Path:
    script_path = Path(__file__).resolve()
    for parent in script_path.parents:
        if (parent / ".git").exists():
            return parent
    return script_path.parents[1]


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a token keyring entry.")
    parser.add_argument("--key-id", default="k1")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    payload = json.dumps(
        {args.key_id: b64encode(secrets.token_bytes(32)).decode("ascii")},
        separators=(",", ":"),
    ) + "\n"
    if args.out is None:
        sys.stdout.write(payload)
        return 0

    output_path = args.out.expanduser().resolve()
    if _is_within(output_path, _repository_root()):
        print(
            "warning: do not store keyrings inside the repository or a shared drive",
            file=sys.stderr,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            descriptor = -1
            output.write(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
