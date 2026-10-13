#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Abu Ali — J.A.R.V.I.S. dashboard backend (local / preview server).

Serves the HUD in `jarvis/index.html` and the JSON API it consumes:

    GET  /                     → the interface
    GET  /api/health           → liveness probe
    GET  /api/state            → everything the HUD renders (one round-trip)
    POST /api/command          → dispatch a real GitHub Actions run
    POST /api/install-workflow → write .github/workflows/abu_ali_agent.yml

Two modes, chosen automatically:

* **live**  — when MY_GITHUB_TOKEN + GITHUB_REPOSITORY are present the server
              reads real issues / tasks / memory / deployments and really
              dispatches workflows.
* **demo**  — otherwise it serves rich sample data so the interface can be
              inspected and demoed anywhere without a single credential.

Standard library only (no pip install needed):

    python jarvis/server.py                     # auto: live if configured
    python jarvis/server.py --demo --port 8080
    python jarvis/server.py --tunnel-note       # print the LAN URL for phones

Security: bind is 0.0.0.0 and every response is built from JSON payloads with
strict escaping on the client, so a malicious issue title cannot inject markup.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
INDEX = Path(__file__).resolve().parent / "index.html"
WORKFLOW_SOURCE = ROOT / "deploy" / "abu_ali_agent.yml"
WORKFLOW_TARGET = ".github/workflows/abu_ali_agent.yml"

API = os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/")
SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
GITHUB_TOKEN = os.getenv("MY_GITHUB_TOKEN") or ""
REPO = os.getenv("GITHUB_REPOSITORY") or ""
WORKFLOW_FILE = os.getenv("ABU_WORKFLOW_FILE", "abu_ali_agent.yml")
TASK_LABELS = [x.strip() for x in (os.getenv("TASK_LABELS") or "task,devops").split(",") if x.strip()]

_CACHE: Dict[str, Any] = {"at": 0.0, "payload": None}
_CACHE_TTL = float(os.getenv("JARVIS_CACHE_TTL", "12"))
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
#  HTTP helpers
# ---------------------------------------------------------------------------

def http_json(method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
              payload: Any = None, timeout: int = 25) -> tuple[int, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    request.add_header("User-Agent", "abu-ali-jarvis")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"raw": body[:400]}
    except Exception as exc:  # network, DNS, TLS …
        return 0, {"error": str(exc)}


def github_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def supabase_headers() -> Dict[str, str]:
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def is_live() -> bool:
    return bool(GITHUB_TOKEN and REPO and "/" in REPO)


# ---------------------------------------------------------------------------
#  Live data
# ---------------------------------------------------------------------------

def fetch_issues(limit: int = 12) -> List[Dict[str, Any]]:
    labels = ",".join(TASK_LABELS) if TASK_LABELS else None
    url = f"{API}/repos/{REPO}/issues?state=open&sort=updated&direction=desc&per_page={limit}"
    if labels:
        url += f"&labels={urllib.parse.quote(labels)}"
    status, body = http_json("GET", url, headers=github_headers())
    if status != 200 or not isinstance(body, list):
        return []
    issues = []
    for item in body:
        if "pull_request" in item:
            continue
        issues.append({
            "number": item.get("number"),
            "title": item.get("title") or "",
            "labels": [str((label or {}).get("name") or "") for label in (item.get("labels") or [])],
            "url": item.get("html_url") or "",
            "updated_at": item.get("updated_at"),
        })
    return issues[:limit]


def supabase_rows(table: str, params: str, limit: int = 20) -> List[Dict[str, Any]]:
    if not (SUPABASE_URL and SUPABASE_KEY):
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}&limit={limit}"
    status, body = http_json("GET", url, headers=supabase_headers())
    return body if status in (200, 206) and isinstance(body, list) else []


def fetch_state() -> Dict[str, Any]:
    """One aggregated payload for the HUD (cached briefly to spare the APIs)."""
    with _LOCK:
        if _CACHE["payload"] is not None and (time.time() - _CACHE["at"]) < _CACHE_TTL:
            return _CACHE["payload"]

    live = is_live()
    if not live:
        payload = demo_state()
    else:
        tasks = supabase_rows(
            "tasks_log",
            "select=id,created_at,issue_number,issue_title,status,summary,pr_url,pr_number,llm_provider,llm_model",
            14,
        ) or []
        memory = supabase_rows(
            "system_memory",
            "select=kind,key,content,importance,updated_at&order=importance.desc,updated_at.desc",
            12,
        ) or []
        deploys = supabase_rows(
            "deployments",
            "select=target_name,target_type,status,url,created_at&order=id.desc",
            10,
        ) or []
        issues = fetch_issues(12)

        completed = len([t for t in tasks if str(t.get("status")) == "completed"])
        success = int(round(completed / len(tasks) * 100)) if tasks else None
        runs: List[Dict[str, Any]] = []
        for task in tasks[:8]:
            status = str(task.get("status") or "")
            kind = "fail" if "fail" in status else ("run" if "run" in status else "ok")
            runs.append({
                "kind": kind,
                "title": f"#{task.get('issue_number') or '—'} {task.get('issue_title') or task.get('summary') or ''}",
                "meta": " · ".join(x for x in [task.get("llm_model"), status] if x),
                "at": task.get("created_at"),
            })

        stats: Dict[str, Any] = {
            "tasks": len(tasks),
            "success": success,
            "prs": len([t for t in tasks if t.get("pr_url")]),
            "deploys": len(deploys),
        }
        vitals = build_vitals(live=True)
        payload = {
            "mode": "live",
            "repo": REPO,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "vitals": vitals,
            "stats": stats,
            "tasks": [
                {
                    "id": t.get("id"),
                    "issue": t.get("issue_number"),
                    "title": t.get("issue_title") or t.get("summary") or "",
                    "status": t.get("status"),
                    "provider": t.get("llm_provider"),
                    "model": t.get("llm_model"),
                    "pr_url": t.get("pr_url") or "",
                    "created_at": t.get("created_at"),
                }
                for t in tasks
            ],
            "memory": memory,
            "deploys": [
                {
                    "target": d.get("target_name"),
                    "type": d.get("target_type"),
                    "status": d.get("status"),
                    "url": d.get("url") or "",
                    "created_at": d.get("created_at"),
                }
                for d in deploys
            ],
            "issues": issues,
            "runs": runs,
        }

    with _LOCK:
        _CACHE["at"] = time.time()
        _CACHE["payload"] = payload
    return payload


def build_vitals(live: bool) -> List[Dict[str, Any]]:
    def state(*names: str, optional: bool = False) -> str:
        present = any(os.getenv(name) for name in names)
        if present:
            return "ok"
        return "warn" if optional else "off"

    return [
        {"name": "GitHub", "sub": "MY_GITHUB_TOKEN", "state": state("MY_GITHUB_TOKEN"),
         "value": 100 if os.getenv("MY_GITHUB_TOKEN") else 6, "note": os.getenv("GITHUB_REPOSITORY", "")},
        {"name": "Groq", "sub": os.getenv("GROQ_MODELS", "llama-3.3-70b-versatile").split(",")[0],
         "state": state("GROQ_API_KEY"), "value": 92 if os.getenv("GROQ_API_KEY") else 6, "note": "fast cortex"},
        {"name": "Gemini", "sub": os.getenv("GEMINI_MODELS", "gemini-2.0-flash").split(",")[0],
         "state": state("GEMINI_API_KEY"), "value": 88 if os.getenv("GEMINI_API_KEY") else 6, "note": "deep cortex"},
        {"name": "Supabase", "sub": "tasks_log · system_memory",
         "state": state("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY") if os.getenv("SUPABASE_URL") and SUPABASE_KEY else "off",
         "value": 94 if (os.getenv("SUPABASE_URL") and SUPABASE_KEY) else 6, "note": "long-term memory"},
        {"name": "Cloudflare", "sub": "workers · pages",
         "state": state("CLOUDFLARE_API_TOKEN", optional=True) if not os.getenv("CLOUDFLARE_API_TOKEN") else "ok",
         "value": 76 if os.getenv("CLOUDFLARE_API_TOKEN") else 20, "note": "edge link"},
        {"name": "MCP", "sub": "context7 · github · supabase",
         "state": "ok" if os.getenv("MCP_ENABLE_CONTEXT7", "true") == "true" else "warn",
         "value": 62, "note": "model context protocol"},
        {"name": "Dashboard", "sub": "live" if live else "demo", "state": "ok" if live else "warn",
         "value": 100 if live else 45, "note": REPO},
    ]


# ---------------------------------------------------------------------------
#  Demo data (no credentials required)
# ---------------------------------------------------------------------------

def demo_state() -> Dict[str, Any]:
    now = datetime.now(timezone.utc)

    def ago(minutes: int) -> str:
        return (now - timedelta(minutes=minutes)).isoformat()

    return {
        "mode": "demo",
        "repo": REPO or "owner/Abu-Ali (demo)",
        "generated_at": now.isoformat(),
        "vitals": build_vitals(live=False),
        "stats": {"tasks": 48, "success": 92, "prs": 17, "deploys": 5},
        "tasks": [
            {"id": 48, "issue": 42, "title": "إضافة endpoint للفحص الصحي", "status": "completed",
             "provider": "gemini", "model": "gemini-2.0-flash",
             "pr_url": "https://github.com/owner/repo/pull/42", "created_at": ago(6)},
            {"id": 47, "issue": 41, "title": "تحديث واجهة لوحة التحكم", "status": "running",
             "provider": "groq", "model": "llama-3.3-70b-versatile", "pr_url": "", "created_at": ago(18)},
            {"id": 46, "issue": 40, "title": "نشر Worker على Cloudflare", "status": "completed",
             "provider": "groq", "model": "llama-3.3-70b-versatile",
             "pr_url": "https://github.com/owner/repo/pull/40", "created_at": ago(75)},
            {"id": 45, "issue": 39, "title": "إصلاح خطأ في تسجيل الدخول", "status": "failed",
             "provider": "gemini", "model": "gemini-1.5-pro", "pr_url": "", "created_at": ago(160)},
            {"id": 44, "issue": 38, "title": "توليد اختبارات الوحدة للدفع", "status": "completed",
             "provider": "gemini", "model": "gemini-2.0-flash",
             "pr_url": "https://github.com/owner/repo/pull/38", "created_at": ago(320)},
        ],
        "memory": [
            {"kind": "architecture", "key": "health-endpoint",
             "content": "كل فحوصات الصحة في health/check.py وتُسجَّل في app.py", "importance": 7,
             "updated_at": ago(6)},
            {"kind": "preference", "key": "language-arabic",
             "content": "المالك يفضّل كل التقارير بالعربية مع روابط مباشرة", "importance": 9, "updated_at": ago(60)},
            {"kind": "decision", "key": "edge-workers",
             "content": "الواجهة تُنشر كـ Cloudflare Worker باسم abu-ali-jarvis", "importance": 8, "updated_at": ago(180)},
            {"kind": "lesson", "key": "ci-permissions",
             "content": "GitHub يرفض كتابة .github/workflows من توكن بلا صلاحية workflow",
             "importance": 6, "updated_at": ago(240)},
        ],
        "deploys": [
            {"target": "abu-ali-jarvis", "type": "worker", "status": "deployed",
             "url": "https://abu-ali-jarvis.workers.dev", "created_at": ago(30)},
            {"target": "abu-ali-site", "type": "pages", "status": "deployed",
             "url": "https://abu-ali-site.pages.dev", "created_at": ago(200)},
        ],
        "issues": [
            {"number": 42, "title": "أضف endpoint للفحص الصحي", "labels": ["task"],
             "url": "https://github.com/owner/repo/issues/42", "updated_at": ago(9)},
            {"number": 41, "title": "حدّث واجهة لوحة التحكم", "labels": ["task", "devops"],
             "url": "https://github.com/owner/repo/issues/41", "updated_at": ago(22)},
            {"number": 37, "title": "وثّق أوامر الوكيل", "labels": ["task"],
             "url": "https://github.com/owner/repo/issues/37", "updated_at": ago(400)},
        ],
        "runs": [
            {"kind": "ok", "title": "مهمة #42 أُنجزت — PR مفتوح", "meta": "gemini-2.0-flash · completed", "at": ago(6)},
            {"kind": "run", "title": "مهمة #41 قيد التنفيذ…", "meta": "llama-3.3-70b · running", "at": ago(18)},
            {"kind": "ok", "title": "نشر Worker: abu-ali-jarvis", "meta": "cloudflare · deployed", "at": ago(30)},
            {"kind": "info", "title": "الواجهة تعمل في وضع العرض (بلا مفاتيح)", "meta": "demo", "at": ago(1)},
        ],
    }


# ---------------------------------------------------------------------------
#  Actions
# ---------------------------------------------------------------------------

def dispatch_command(instruction: str, issue: Optional[str] = None) -> Dict[str, Any]:
    if not is_live():
        return {"ok": False, "demo": True,
                "message": "وضع العرض: أضف MY_GITHUB_TOKEN و GITHUB_REPOSITORY لتنفيذ حقيقي."}
    payload: Dict[str, Any] = {"ref": os.getenv("ABU_DEFAULT_BRANCH", "main"),
                               "inputs": {"instruction": instruction, "dry_run": "false"}}
    if issue:
        payload["inputs"]["issue_number"] = str(issue)
    status, body = http_json(
        "POST", f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        headers=github_headers(), payload=payload,
    )
    if status in (201, 204):
        runs_url = f"https://github.com/{REPO}/actions/workflows/{WORKFLOW_FILE}"
        record_interaction(instruction)
        return {"ok": True, "message": "أُرسل الأمر إلى GitHub Actions — سيبدأ التنفيذ خلال ثوانٍ.",
                "run_url": runs_url, "issue": issue}
    message = (body or {}).get("message") if isinstance(body, dict) else str(body)[:200]
    hint = ""
    if status == 404:
        hint = " (تحقق أن ملف الـ Workflow موجود على الفرع الافتراضي وأن التوكن يملك صلاحية workflow)"
    return {"ok": False, "message": f"فشل الإرسال ({status}): {message}{hint}", "detail": body}


def record_interaction(text: str) -> None:
    if not (SUPABASE_URL and SUPABASE_KEY):
        return
    http_json(
        "POST", f"{SUPABASE_URL}/rest/v1/interactions",
        headers={**supabase_headers(), "Prefer": "return=minimal"},
        payload={"repo": REPO, "author": os.getenv("GITHUB_ACTOR", "jarvis-ui"),
                 "role": "user", "body": text[:8000], "metadata": {"source": "jarvis-dashboard"}},
    )


def strip_workflow_header(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("#"):
        for index, line in enumerate(lines[1:], start=1):
            if line.startswith("# ===="):
                lines = lines[index + 1:]
                break
    return "".join(lines).lstrip("\n")


def install_workflow() -> Dict[str, Any]:
    if not is_live():
        return {"ok": False, "message": "وضع العرض: التثبيت يحتاج توكن GitHub حقيقي (صلاحية workflow)."}
    if not WORKFLOW_SOURCE.exists():
        return {"ok": False, "message": f"لا يوجد {WORKFLOW_SOURCE.relative_to(ROOT)}"}
    body = strip_workflow_header(WORKFLOW_SOURCE.read_text(encoding="utf-8"))
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")

    status, current = http_json("GET", f"{API}/repos/{REPO}/contents/{WORKFLOW_TARGET}",
                                headers=github_headers())
    existing_sha = current.get("sha") if status == 200 and isinstance(current, dict) else None

    payload = {"message": "ci: install the Abu Ali orchestrator workflow [skip ci]",
               "content": encoded, "branch": os.getenv("ABU_DEFAULT_BRANCH", "main")}
    if existing_sha:
        payload["sha"] = existing_sha
    status, body = http_json("PUT", f"{API}/repos/{REPO}/contents/{WORKFLOW_TARGET}",
                             headers=github_headers(), payload=payload)
    if status in (200, 201):
        url = ((body or {}).get("content") or {}).get("html_url") or f"https://github.com/{REPO}/blob/main/{WORKFLOW_TARGET}"
        return {"ok": True, "message": "تم تثبيت ملف الـ Workflow ✔", "url": url}
    message = (body or {}).get("message") if isinstance(body, dict) else str(body)[:200]
    hint = " — التوكن يحتاج صلاحية `workflow`" if status == 403 else ""
    return {"ok": False, "message": f"فشل التثبيت ({status}): {message}{hint}"}


# ---------------------------------------------------------------------------
#  HTTP server
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "AbuAliJarvis/1.0"

    # quiet, single-line logging
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        sys.stderr.write("  · %s %s\n" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            parsed = json.loads(raw.decode("utf-8") or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/", "/index.html", "/jarvis"):
            try:
                html = INDEX.read_text(encoding="utf-8")
            except Exception as exc:
                self._send(500, f"index.html missing: {exc}".encode(), "text/plain; charset=utf-8")
                return
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/health":
            self._json({"ok": True, "mode": "live" if is_live() else "demo",
                        "repo": REPO, "at": datetime.now(timezone.utc).isoformat()})
        elif path == "/api/state":
            try:
                self._json(fetch_state())
            except Exception as exc:
                self._json({"mode": "demo", **demo_state(), "error": str(exc)})
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"ok": False, "message": f"unknown route {path}"}, 404)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        body = self._read_body()
        if path == "/api/command":
            instruction = str(body.get("instruction") or "").strip()
            issue = body.get("issue")
            if not instruction:
                self._json({"ok": False, "message": "الأمر فارغ"}, 400)
                return
            try:
                self._json(dispatch_command(instruction, str(issue) if issue else None))
            except Exception as exc:
                self._json({"ok": False, "message": f"خطأ داخلي: {exc}"}, 500)
        elif path == "/api/install-workflow":
            try:
                self._json(install_workflow())
            except Exception as exc:
                self._json({"ok": False, "message": f"خطأ داخلي: {exc}"}, 500)
        else:
            self._json({"ok": False, "message": f"unknown route {path}"}, 404)


def lan_ip() -> str:
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except Exception:
        return "127.0.0.1"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Abu Ali J.A.R.V.I.S. dashboard server")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    parser.add_argument("--demo", action="store_true", help="force demo mode even if credentials exist")
    parser.add_argument("--no-lan-note", action="store_true", help="hide the LAN URL hint")
    args = parser.parse_args(argv)

    if args.demo:
        globals()["GITHUB_TOKEN"] = ""

    if not INDEX.exists():
        print(f"✘ {INDEX} is missing — the interface file is required")
        return 1

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    mode = "LIVE" if is_live() else "DEMO"
    print("\n" + "═" * 66)
    print(f"  ABU ALI · J.A.R.V.I.S. CONTROL  —  {mode} MODE")
    print("═" * 66)
    print(f"  local    : http://127.0.0.1:{args.port}")
    if not args.no_lan_note:
        print(f"  phone    : http://{lan_ip()}:{args.port}   (same Wi-Fi)")
    print(f"  repo     : {REPO or '(not configured — demo data)'}")
    print(f"  supabase : {'connected' if (SUPABASE_URL and SUPABASE_KEY) else 'not configured'}")
    print(f"  workflow : {WORKFLOW_FILE}")
    print("═" * 66 + "\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[!] shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
