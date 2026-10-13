#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Install (or update) the Abu Ali GitHub Actions workflow.

WHY THIS SCRIPT EXISTS
----------------------
GitHub refuses to let a GitHub App — or any token without the `workflow` scope —
create or update files inside `.github/workflows/`. Repository automation often
runs with such a token, so this script installs the workflow through the REST
API using *your* token (which does have the scope), and then verifies that the
installed copy matches `deploy/abu_ali_agent.yml`.

USAGE
-----
    # token from the environment (recommended)
    export MY_GITHUB_TOKEN=ghp_xxxxxxxx
    python scripts/install_workflow.py

    # …or explicitly, targeting a branch
    python scripts/install_workflow.py --token ghp_xxx --branch main

    # check only (no writes): is the installed workflow up to date?
    python scripts/install_workflow.py --check

    # install into the current session branch instead of the default branch
    python scripts/install_workflow.py --branch arena/8c86bf3f-abu-ali

Nothing else is required: the script uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "deploy" / "abu_ali_agent.yml"
TARGET_PATH = ".github/workflows/abu_ali_agent.yml"
API = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def log(message: str) -> None:
    print(message, flush=True)


def api(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", "abu-ali-installer")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            body = response.read().decode("utf-8") or "{}"
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8") or "{}"
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body}
        return exc.code, parsed
    except urllib.error.URLError as exc:
        return 0, {"message": f"network error: {exc}"}


def workflow_body() -> str:
    """The workflow exactly as GitHub should run it (header comment stripped)."""
    if not SOURCE.exists():
        raise SystemExit(f"missing {SOURCE.relative_to(ROOT)} — regenerate it from the repository copy")
    text = SOURCE.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("#"):
        # drop the "CANONICAL COPY" header comment block up to the closing rule
        closing = next((i for i, line in enumerate(lines[1:], start=1) if line.startswith("# ====")), None)
        if closing is not None:
            lines = lines[closing + 1 :]
    return "".join(lines).lstrip("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the Abu Ali agent workflow into .github/workflows/")
    parser.add_argument("--token", default=os.getenv("MY_GITHUB_TOKEN") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN"),
                        help="GitHub PAT with `repo` + `workflow` scopes (default: $MY_GITHUB_TOKEN)")
    parser.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", ""), help="owner/name (default: $GITHUB_REPOSITORY)")
    parser.add_argument("--branch", default="", help="target branch (default: the repository default branch)")
    parser.add_argument("--check", action="store_true", help="only verify that the installed copy matches")
    args = parser.parse_args(argv)

    body = workflow_body()
    local_b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")

    repo = args.repo.strip()
    if not repo or "/" not in repo:
        log("✘ repository is unknown — pass --repo owner/name or set GITHUB_REPOSITORY")
        return 2

    if not args.token:
        log("✘ no token — set MY_GITHUB_TOKEN (needs `repo` + `workflow` scopes) or pass --token")
        return 2

    branch = args.branch.strip()
    if not branch:
        status, info = api("GET", f"{API}/repos/{repo}", args.token)
        if status != 200:
            log(f"✘ cannot read the repository ({status}: {info.get('message')})")
            return 1
        branch = info.get("default_branch") or "main"
    log(f"→ target: {repo} @ {branch}  ({TARGET_PATH})")

    # what is installed right now?
    status, current = api("GET", f"{API}/repos/{repo}/contents/{TARGET_PATH}?ref={branch}", args.token)
    installed_b64 = ""
    if status == 200 and isinstance(current, dict) and current.get("content"):
        installed_b64 = str(current["content"]).replace("\n", "")
    elif status not in (200, 404):
        log(f"⚠ could not read the current workflow ({status}: {current.get('message')}) — attempting the write anyway")

    if installed_b64 == local_b64:
        log("✔ the installed workflow is already up to date — nothing to do")
        return 0
    if args.check:
        log(f"✘ {TARGET_PATH} is missing or out of date on {branch} — run without --check to install it")
        return 1

    payload: dict = {
        "message": f"ci: install the Abu Ali orchestrator workflow ({TARGET_PATH}) [skip ci]",
        "content": local_b64,
        "branch": branch,
    }
    if installed_b64:
        payload["sha"] = current.get("sha")
        log("• updating the existing workflow file")
    else:
        log("• creating the workflow file")

    status, result = api("PUT", f"{API}/repos/{repo}/contents/{TARGET_PATH}", args.token, payload)
    if status in (200, 201):
        url = ((result.get("content") or {}).get("html_url")) or f"https://github.com/{repo}/blob/{branch}/{TARGET_PATH}"
        log(f"✔ installed: {url}")
        log("→ next: add the repository secrets (see README.md) and run the workflow once from the Actions tab")
        return 0

    message = result.get("message", "unknown error")
    log(f"✘ install failed ({status}: {message})")
    if status == 403:
        log("  The token is missing the `workflow` scope. Create a classic PAT with `repo` + `workflow`,")
        log("  or add the file from the GitHub mobile app: Add file → Create new file → paste")
        log(f"  {SOURCE.relative_to(ROOT)} as {TARGET_PATH}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
