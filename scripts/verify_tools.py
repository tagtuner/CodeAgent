#!/usr/bin/env python3
"""Smoke-test CodeAgent tools (safe read-only-ish calls). Run on server: python3 scripts/verify_tools.py"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Repo root = parent of scripts/
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.config import Config
from main import build_registry
from tools.base import ToolRegistry


def _ok(name: str, result: object, expect_err: bool = False) -> None:
    text = result if isinstance(result, str) else str(result)
    low = text.lower()
    errish = "error" in low[:120] or "invalid" in low[:80] or "not installed" in low
    if expect_err:
        status = "PASS" if errish or len(text) > 0 else "FAIL"
    else:
        status = "FAIL" if ("error: httpx" in low or text.startswith("Unknown tool")) else "PASS"
        if "error" in low[:200] and not expect_err and name not in (
            "oracle_query",
            "oracle_schema",
            "sql_validate",
            "oracle_explain",
            "ebs_concurrent_status",
        ):
            status = "FAIL"
    print(f"{status:4}  {name}")
    if status == "FAIL" or (expect_err and "PASS" in status):
        for line in text.splitlines()[:8]:
            print(f"      | {line[:200]}")


async def run_one(reg: ToolRegistry, name: str, args: dict, expect_err: bool = False) -> None:
    tool = reg.get(name)
    if not tool:
        print(f"FAIL  {name} (not registered)")
        return
    try:
        out = await reg.execute(name, args)
    except Exception as e:
        print(f"FAIL  {name} (exception: {e})")
        return
    _ok(name, out, expect_err=expect_err)


async def main() -> int:
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else str(_ROOT / "config.yaml")
    if not Path(cfg_path).is_file():
        print(f"FAIL  config missing: {cfg_path}")
        return 1
    config = Config.load(cfg_path)
    reg = build_registry(config)
    names = reg.list_tools()
    print(f"Registered ({len(names)}): {', '.join(sorted(names))}\n")

    await run_one(reg, "bash", {"command": "echo CODEAGENT_VERIFY_OK"})
    await run_one(
        reg,
        "read_file",
        {"path": "/opt/codeagent/config.yaml", "offset": 0, "limit": 15},
    )
    tmp = "/tmp/codeagent_verify_tools.txt"
    await run_one(
        reg,
        "write_file",
        {"path": tmp, "content": "verify\n"},
    )
    await run_one(reg, "read_file", {"path": tmp, "offset": 0, "limit": 5})
    await run_one(
        reg,
        "glob_search",
        {"pattern": "*.yaml", "directory": "/opt/codeagent"},
    )
    await run_one(reg, "git_status", {"directory": "/opt/codeagent"})
    await run_one(reg, "git_diff", {"directory": "/opt/codeagent", "staged": False})
    await run_one(
        reg,
        "git_commit",
        {"message": "codeagent verify (no-op)", "directory": "/opt/codeagent"},
        expect_err=True,
    )

    await run_one(reg, "web_search", {"query": "Karachi weather today", "max_results": 2})
    await run_one(reg, "web_fetch", {"url": "https://example.com", "max_chars": 800})

    await run_one(reg, "ebs_module_guide", {"module": "PO"})
    await run_one(
        reg,
        "ebs_concurrent_status",
        {"phase": "ALL", "limit": 5, "last_hours": 24},
        expect_err=True,
    )

    await run_one(reg, "sql_validate", {"sql": "SELECT 1 FROM DUAL"}, expect_err=True)
    await run_one(reg, "oracle_query", {"sql": "SELECT 1 FROM DUAL WHERE 1=0"}, expect_err=True)
    await run_one(reg, "oracle_schema", {"table_name": "DUAL", "db": "dev"}, expect_err=True)
    await run_one(reg, "oracle_explain", {"sql": "SELECT 1 FROM DUAL"}, expect_err=True)

    # edit_file: restore-style noop risk — use temp file
    await run_one(
        reg,
        "edit_file",
        {
            "path": tmp,
            "old_string": "verify\n",
            "new_string": "verify_edited\n",
        },
    )

    print("\nNote: Oracle/EBS concurrent may FAIL or show errors if DB not configured — tools still load.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
