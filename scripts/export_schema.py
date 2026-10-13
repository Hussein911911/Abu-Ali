#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Regenerate `db/schema.sql` from the single source of truth in `main.py`
(the `SCHEMA_SQL` constant) so the two can never drift apart.

Usage:
    python scripts/export_schema.py            # write db/schema.sql
    python scripts/export_schema.py --check    # exit 1 if db/schema.sql is stale
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
TARGET = ROOT / "db" / "schema.sql"

HEADER = """-- =============================================================================
--  Abu Ali — Supabase schema for the agent's long-term memory.
--
--  THIS FILE IS GENERATED. Do not edit it by hand.
--  Source of truth : SCHEMA_SQL inside main.py
--  Regenerate with : python scripts/export_schema.py
--
--  Safe to run repeatedly: every statement is CREATE ... IF NOT EXISTS or
--  CREATE OR REPLACE. Nothing here ever DROPs a table or deletes a row, so
--  existing features, roles and data are always preserved.
--
--  Apply it once in the Supabase SQL editor (Project → SQL Editor → Run),
--  or let the agent self-heal when the project exposes an `exec_sql` RPC.
-- =============================================================================

"""


def extract_schema() -> str:
    source = MAIN.read_text(encoding="utf-8")
    match = re.search(r'SCHEMA_SQL = """(.*?)"""\.strip\(\)', source, re.DOTALL)
    if not match:
        raise SystemExit("SCHEMA_SQL constant not found in main.py")
    return match.group(1).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the Abu Ali Supabase schema")
    parser.add_argument("--check", action="store_true", help="verify db/schema.sql is up to date")
    args = parser.parse_args(argv)

    body = extract_schema()
    expected = HEADER + body

    if args.check:
        if not TARGET.exists():
            print("db/schema.sql is missing")
            return 1
        if TARGET.read_text(encoding="utf-8") != expected:
            print("db/schema.sql is STALE — run: python scripts/export_schema.py")
            return 1
        print("db/schema.sql is up to date ✔")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(expected, encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(expected)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
