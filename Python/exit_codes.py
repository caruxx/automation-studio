#!/usr/bin/env python3
"""終了コードの単一正本（workflow.json の exit_reasons）。

pipeline の sys.exit、orchestrator の Exit、batch の戻り値、studio.py の emit、
error_humanizer の説明は全てここを参照する。workflow.json が読めない場合は
組み込みの既定表で動作し、起動は止めない（不一致はテストで検出する）。
"""
from __future__ import annotations

import json
import sys
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional

WORKFLOW_JSON = Path(__file__).resolve().parent / "workflow.json"

# 組み込みの既定表（workflow.json と同じ内容。JSON が壊れていても判定を続けるため）
_DEFAULT_EXIT_REASONS: list[dict[str, Any]] = [
    {"reason": "ok",                "exit_code": 0,  "status": "done",    "retryable": False, "needs_human": False},
    {"reason": "failed",            "exit_code": 1,  "status": "failed",  "retryable": False, "needs_human": False},
    {"reason": "usage",             "exit_code": 2,  "status": "failed",  "retryable": False, "needs_human": True},
    {"reason": "unattended_login",  "exit_code": 75, "status": "failed",  "retryable": False, "needs_human": True},
    {"reason": "retryable",         "exit_code": 76, "status": "failed",  "retryable": True,  "needs_human": False},
    {"reason": "quota_exhausted",   "exit_code": 77, "status": "failed",  "retryable": True,  "needs_human": False},
    {"reason": "preflight_fail",    "exit_code": 78, "status": "failed",  "retryable": False, "needs_human": True},
    {"reason": "selection_pending", "exit_code": 79, "status": "blocked", "retryable": False, "needs_human": False},
]


class ExitCode(IntEnum):
    OK = 0
    FAIL = 1
    USAGE = 2
    UNATTENDED_LOGIN = 75
    RETRYABLE = 76
    QUOTA_EXHAUSTED = 77
    PREFLIGHT_FAIL = 78
    SELECTION_PENDING = 79


def load_workflow(path: Path = WORKFLOW_JSON) -> dict[str, Any]:
    """workflow.json を読む。読めなければ既定表だけを持つ辞書を返す。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("exit_reasons"), list):
            raise ValueError("exit_reasons がありません")
        return data
    except Exception as e:  # noqa: BLE001
        print(f"[exit_codes] workflow.json を読めないため既定表を使います: {e}", file=sys.stderr)
        return {"schema_version": 1, "workflow_version": "builtin", "exit_reasons": list(_DEFAULT_EXIT_REASONS)}


_WORKFLOW = load_workflow()
WORKFLOW_VERSION: str = str(_WORKFLOW.get("workflow_version") or "builtin")
EXIT_REASONS: list[dict[str, Any]] = list(_WORKFLOW["exit_reasons"])
_BY_CODE: dict[int, dict[str, Any]] = {int(r["exit_code"]): r for r in EXIT_REASONS if "exit_code" in r}
_BY_REASON: dict[str, dict[str, Any]] = {str(r["reason"]): r for r in EXIT_REASONS if "reason" in r}


def describe(code: int) -> dict[str, Any]:
    """終了コードの定義行を返す。未定義のコードは一般失敗として扱う。"""
    row = _BY_CODE.get(int(code))
    if row is None:
        return {"reason": "failed", "exit_code": int(code), "status": "failed", "retryable": False, "needs_human": False, "known": False}
    return {**row, "known": True}


def reason_for(code: int) -> str:
    return str(describe(code)["reason"])


def status_for(code: int) -> str:
    """done / failed / blocked。blocked は待機であり失敗に数えない。"""
    return str(describe(code)["status"])


def code_for(reason: str) -> Optional[int]:
    row = _BY_REASON.get(reason)
    return int(row["exit_code"]) if row else None


def is_blocked(code: int) -> bool:
    return status_for(code) == "blocked"


def is_retryable(code: int) -> bool:
    return bool(describe(code).get("retryable"))


def needs_human(code: int) -> bool:
    return bool(describe(code).get("needs_human"))


def ledger_status_for(code: int) -> str:
    """台帳 finish_run の status（done / failed / waiting_selection）。"""
    st = status_for(code)
    if st == "done":
        return "done"
    if st == "blocked":
        return "waiting_selection"
    return "failed"


def builtin_matches_workflow() -> list[str]:
    """組み込み既定表・ExitCode・workflow.json の不一致を列挙する（テスト用）。"""
    problems: list[str] = []
    json_codes = {int(r["exit_code"]): r for r in EXIT_REASONS}
    for row in _DEFAULT_EXIT_REASONS:
        j = json_codes.get(row["exit_code"])
        if j is None:
            problems.append(f"workflow.json に exit_code {row['exit_code']} がありません")
            continue
        for k in ("reason", "status", "retryable", "needs_human"):
            if j.get(k) != row[k]:
                problems.append(f"exit_code {row['exit_code']} の {k}: json={j.get(k)!r} builtin={row[k]!r}")
    for extra in set(json_codes) - {r["exit_code"] for r in _DEFAULT_EXIT_REASONS}:
        problems.append(f"workflow.json にだけある exit_code {extra}")
    enum_codes = {int(m) for m in ExitCode}
    for c in enum_codes ^ set(json_codes):
        problems.append(f"ExitCode と workflow.json の集合差: {c}")
    return problems
