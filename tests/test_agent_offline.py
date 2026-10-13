#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Abu Ali — offline integration test.

Simulates GitHub, Supabase, Groq/Gemini and Cloudflare at the HTTP layer and
runs a complete agent task, then asserts the real side effects:

  issue  →  branch  →  blobs/tree/commit  →  Pull Request  →  comment  →  memory

No network access and no real credentials are required. Run it in CI:

    python tests/test_agent_offline.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- fake environment (fake values only — never real secrets) -------------
os.environ.update({
    "SUPABASE_URL": "https://fake-project.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "fake-service-role-key-0000000000",
    "GROQ_API_KEY": "fake-groq-key-0000000000",
    "GEMINI_API_KEY": "fake-gemini-key-0000000000",
    "MY_GITHUB_TOKEN": "fake-github-token-0000000000",
    "CLOUDFLARE_ACCOUNT_ID": "fake-account-id",
    "CLOUDFLARE_API_TOKEN": "fake-cf-token-0000000000",
    "GITHUB_REPOSITORY": "owner/demo-repo",
    "GITHUB_EVENT_NAME": "issues",
    "ABU_LOG_FILE": ".abu-ali/test-run.log",
    "ABU_OUTBOX_PATH": ".abu-ali/test-outbox.jsonl",
    "AUTO_DEPLOY": "false",
})

import main as abu  # noqa: E402


BASE_HEAD_SHA = "a" * 40
COMMIT_SHA = "c" * 40
BLOB_SHA = "b" * 40
TREE_SHA = "d" * 40


class FakeResponse:
    def __init__(self, status: int, payload: Any = None, headers: Optional[Dict[str, str]] = None) -> None:
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload, ensure_ascii=False) if payload is not None else ""

    def json(self) -> Any:
        return self._payload


class FakeRouter:
    """Stateful HTTP router that mimics GitHub, Supabase, Groq and Cloudflare."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.refs: Dict[str, str] = {"main": BASE_HEAD_SHA}
        self.issues: Dict[int, Dict[str, Any]] = {}
        self.comments: List[Dict[str, Any]] = []
        self.commits: List[Dict[str, Any]] = []
        self.prs: List[Dict[str, Any]] = []
        self.supabase_rows: Dict[str, List[Dict[str, Any]]] = {
            "tasks_log": [], "system_memory": [], "interactions": [], "deployments": []
        }
        self.llm_turn = 0
        self.uploaded_files: Dict[str, str] = {}
        self.next_task_id = 1

    # -- helpers -----------------------------------------------------------
    def add_issue(self, number: int, title: str, body: str, labels: List[str]) -> None:
        self.issues[number] = {
            "number": number, "title": title, "body": body, "state": "open",
            "labels": [{"name": name} for name in labels],
            "user": {"login": "hussein"}, "html_url": f"https://github.com/owner/demo-repo/issues/{number}",
        }

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:  # noqa: C901 - router
        method = method.upper()
        body = kwargs.get("json")
        self.calls.append({"method": method, "url": url, "body": body, "params": kwargs.get("params")})

        # ---------------- Supabase REST ---------------------------------
        if "/rest/v1/" in url:
            table = url.split("/rest/v1/")[1].split("?")[0]
            if table.startswith("rpc/"):
                return FakeResponse(200, {"ok": True})
            if method == "GET":
                rows = self.supabase_rows.get(table, [])
                params = {key: str(value)[3:] for key, value in (kwargs.get("params") or {}).items()
                          if str(value).startswith("eq.")}
                matched = [row for row in rows if all(str(row.get(key)) == value for key, value in params.items())]
                if table == "tasks_log":
                    return FakeResponse(200, [{"id": row.get("id")} for row in matched[-1:]])
                return FakeResponse(200, matched[:1])
            if method == "POST":
                rows = body if isinstance(body, list) else [body]
                for row in rows:
                    row = dict(row)
                    if table == "tasks_log":
                        row["id"] = self.next_task_id
                        self.next_task_id += 1
                    self.supabase_rows.setdefault(table, []).append(row)
                return FakeResponse(201, rows)
            if method == "PATCH":
                # emulate PostgREST: apply the update to every row matching the eq.* filters
                filters = {key: str(value)[3:] for key, value in (kwargs.get("params") or {}).items()
                           if str(value).startswith("eq.")}
                updated = 0
                for row in self.supabase_rows.get(table, []):
                    if all(str(row.get(key)) == value for key, value in filters.items()):
                        row.update(body or {})
                        updated += 1
                return FakeResponse(204, None) if updated or not filters else FakeResponse(404, {"message": "no row"})

        # ---------------- Groq ------------------------------------------
        if "api.groq.com" in url:
            self.llm_turn += 1
            prompt_text = ""
            try:
                prompt_text = str(body["messages"][-1].get("content") or "")
            except Exception:
                prompt_text = ""
            is_sweep = "maintenance sweep" in prompt_text.lower()

            # --- tool-calling turn ---
            if body and body.get("tools"):
                if is_sweep:
                    # a healthy sweep finds nothing to change and submits no files
                    message = {
                        "role": "assistant", "content": "لا شيء مكسور — أسجّل المعرفة فقط.",
                        "tool_calls": [{
                            "id": "call_0", "type": "function",
                            "function": {"name": "submit", "arguments": json.dumps({
                                "commit_message": "chore: sweep", "pr_title": "sweep",
                                "pr_body": "no changes", "summary": "لا تغييرات مطلوبة",
                                "tests": [], "completed": True,
                            })},
                        }],
                    }
                elif self.llm_turn <= 2:
                    message = {
                        "role": "assistant", "content": "أكتب ملف الفحص الصحي.",
                        "tool_calls": [{
                            "id": "call_1", "type": "function",
                            "function": {"name": "write_file", "arguments": json.dumps({
                                "path": "health/check.py", "mode": "create",
                                "content": "def health():\n    return {'status': 'ok'}\n",
                            })},
                        }],
                    }
                else:
                    message = {
                        "role": "assistant", "content": "انتهيت.",
                        "tool_calls": [{
                            "id": "call_2", "type": "function",
                            "function": {"name": "submit", "arguments": json.dumps({
                                "commit_message": "feat: add health check endpoint",
                                "pr_title": "Add health check endpoint",
                                "pr_body": "Automated by the agent.",
                                "summary": "أضفت ملف فحص صحي بسيط.",
                                "tests": ["checked the return payload"],
                                "completed": True,
                            })},
                        }],
                    }
                return FakeResponse(200, {
                    "model": "llama-3.3-70b-versatile",
                    "choices": [{"message": message}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                })

            # --- stage 3: write the issue comment, quoting the real URLs ---
            if "STAGE 3" in prompt_text:
                import re as _re
                urls = _re.findall(r'https://github\.com/[^"\s]+', prompt_text)
                lines = ["✅ **تم التنفيذ**", "", "🔍 فهمت الطلب وأنشأت فحصاً صحياً بسيطاً.", ""]
                for url in dict.fromkeys(urls):
                    lines.append(f"- 🔗 {url}")
                lines += ["", "_🤖 Abu Ali v1.0.0_"]
                return FakeResponse(200, {
                    "model": "llama-3.3-70b-versatile",
                    "choices": [{"message": {"role": "assistant", "content": "\n".join(lines)}}],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 20},
                })

            # --- planning JSON ---
            if is_sweep:
                return FakeResponse(200, {
                    "model": "llama-3.3-70b-versatile",
                    "choices": [{"message": {"role": "assistant", "content": json.dumps({
                        "intent": "صيانة دورية", "task_type": "maintenance", "risk": "low",
                        "needs_human": False, "summary": "لا شيء مكسور، أحصد المعرفة فقط.",
                        "steps": ["read the repo map"], "files_to_change": [], "tests": [],
                        "deployment": None,
                        "memory": [{"kind": "fact", "key": "sweep-note",
                                    "content": "The sweep found the repo healthy.",
                                    "importance": 3, "tags": ["sweep"]}],
                    }, ensure_ascii=False)}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                })
            text = json.dumps({
                "intent": "إضافة فحص صحي",
                "task_type": "code",
                "risk": "low",
                "needs_human": False,
                "summary": "سأنشئ ملف فحص صحي جديد.",
                "steps": ["write file"],
                "files_to_change": [{"path": "health/check.py", "action": "create", "why": "public health probe"}],
                "tests": ["python -c import"],
                "deployment": None,
                "memory": [{"kind": "fact", "key": "health-endpoint",
                            "content": "Health checks live in health/check.py",
                            "importance": 6, "tags": ["architecture"]}],
            }, ensure_ascii=False)
            return FakeResponse(200, {
                "model": "llama-3.3-70b-versatile",
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            })

        # ---------------- Gemini ----------------------------------------
        if "generativelanguage.googleapis.com" in url:
            return FakeResponse(200, {
                "candidates": [{"content": {"parts": [{"text": "✅ **تم**"}]}}],
                "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 5},
            })

        # ---------------- Cloudflare ------------------------------------
        if "api.cloudflare.com" in url:
            return FakeResponse(200, {"success": True, "result": []})

        # ---------------- GitHub ----------------------------------------
        if "api.github.com" in url:
            path = url.split("api.github.com", 1)[1]
            if path == "/repos/owner/demo-repo" or path.startswith("/repos/owner/demo-repo?"):
                return FakeResponse(200, {"default_branch": "main", "full_name": "owner/demo-repo"})
            if path == "/repos/owner/demo-repo/labels" and method == "GET":
                return FakeResponse(200, [])
            if path == "/repos/owner/demo-repo/labels" and method == "POST":
                return FakeResponse(201, {"name": (body or {}).get("name")})
            if "/issues/" in path and "/comments" in path and method == "POST":
                self.comments.append(body)
                return FakeResponse(201, {"id": len(self.comments)})
            if "/issues/" in path and "/comments" in path and method == "GET":
                return FakeResponse(200, [])
            if path.endswith("/issues/7") and method == "GET":
                return FakeResponse(200, self.issues.get(7) or {})
            if path.endswith("/issues/7/labels") and method == "POST":
                return FakeResponse(200, [])
            if "/collaborators/" in path and path.endswith("/permission") and method == "GET":
                user = path.split("/collaborators/", 1)[1].split("/")[0]
                perms = {"hussein": "admin", "random-guy": "read"}
                if user not in perms:
                    return FakeResponse(404, {"message": "Not Found"})
                return FakeResponse(200, {"permission": perms[user], "user": {"login": user}})
            if "/issues/7/labels/" in path and method == "DELETE":
                return FakeResponse(200, [])
            if path.startswith("/repos/owner/demo-repo/issues?") and method == "GET":
                return FakeResponse(200, [self.issues[7]] if 7 in self.issues else [])
            if "/git/ref/heads/" in path and method == "GET":
                branch = path.split("/git/ref/heads/", 1)[1]
                sha = self.refs.get(branch)
                return FakeResponse(200, {"object": {"sha": sha}}) if sha else FakeResponse(404, {"message": "Not Found"})
            if path.endswith("/git/refs") and method == "POST":
                ref = str((body or {}).get("ref", "")).replace("refs/heads/", "")
                self.refs[ref] = str((body or {}).get("sha"))
                return FakeResponse(201, {"ref": ref})
            if "/git/refs/heads/" in path and method == "PATCH":
                branch = path.split("/git/refs/heads/", 1)[1]
                self.refs[branch] = str((body or {}).get("sha"))
                return FakeResponse(200, {"object": {"sha": self.refs[branch]}})
            if "/git/commits/" in path and method == "GET":
                return FakeResponse(200, {"sha": BASE_HEAD_SHA, "tree": {"sha": TREE_SHA}})
            if path.endswith("/git/blobs") and method == "POST":
                return FakeResponse(201, {"sha": BLOB_SHA})
            if path.endswith("/git/trees") and method == "POST":
                return FakeResponse(201, {"sha": TREE_SHA})
            if path.endswith("/git/commits") and method == "POST":
                self.commits.append(body or {})
                return FakeResponse(201, {"sha": COMMIT_SHA})
            if "/contents/" in path and method == "GET":
                return FakeResponse(404, {"message": "Not Found"})
            if path.endswith("/pulls") and method == "GET":
                return FakeResponse(200, [])
            if path.endswith("/pulls") and method == "POST":
                self.prs.append(body or {})
                return FakeResponse(201, {"number": 12, "html_url": "https://github.com/owner/demo-repo/pull/12"})
            return FakeResponse(404, {"message": f"unmapped: {method} {path}"})

        return FakeResponse(404, {"message": f"unmapped host: {url}"})


class TestAgentOffline(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        abu.setup_logging(verbose=False)
        # Keep the test hermetic: force the agent onto the pure-REST paths so the
        # SDKs never reach the network. (CI installs the SDKs; they are optional.)
        abu._PyGithub = None            # type: ignore[assignment]
        abu._GroqClient = None          # type: ignore[assignment]
        abu._genai = None               # type: ignore[assignment]
        abu._genai_modern = None        # type: ignore[assignment]

    def setUp(self) -> None:
        # every test starts from a clean "issues.opened" trigger
        os.environ["GITHUB_EVENT_NAME"] = "issues"
        self.router = FakeRouter()
        self.router.add_issue(
            7,
            "أضف فحص صحي",
            "نريد endpoint للفحص الصحي في health/check.py",
            ["task", "devops"],
        )
        self._original_request = abu.HTTP_SESSION.request
        abu.HTTP_SESSION.request = self.router.request  # type: ignore[assignment]
        # Point the fake event payload at the issue event
        self.event_file = ROOT / ".abu-ali" / "test-event.json"
        self.event_file.parent.mkdir(parents=True, exist_ok=True)
        self.event_file.write_text(json.dumps({"action": "opened", "issue": self.router.issues[7]}), encoding="utf-8")
        os.environ["GITHUB_EVENT_PATH"] = str(self.event_file)
        self.router.commits.clear()

    def _set_comment_event(self, body: str, user: str) -> None:
        os.environ["GITHUB_EVENT_NAME"] = "issue_comment"
        self.event_file.write_text(json.dumps({
            "action": "created",
            "issue": self.router.issues[7],
            "comment": {"id": 555, "body": body, "user": {"login": user, "type": "User"}},
        }), encoding="utf-8")

    def tearDown(self) -> None:
        abu.HTTP_SESSION.request = self._original_request  # type: ignore[assignment]

    # ------------------------------------------------------------------
    def test_full_task_lifecycle(self) -> None:
        cfg = abu.Config.from_env()
        agent = abu.Agent(cfg)
        exit_code = agent.run()
        self.assertEqual(exit_code, 0, "the agent run should not fail")

        # branch created from main
        branches = [name for name in self.router.refs if name.startswith("abu-ali/issue-7")]
        self.assertTrue(branches, f"no working branch was created: {self.router.refs}")
        branch = branches[0]

        # commit created and the ref moved to it
        self.assertEqual(len(self.router.commits), 1, "exactly one commit expected")
        self.assertIn("health check", json.dumps(self.router.commits[0]).lower())
        self.assertEqual(self.router.refs[branch], COMMIT_SHA)

        # pull request opened
        self.assertEqual(len(self.router.prs), 1)
        self.assertEqual(self.router.prs[0]["head"], branch)
        self.assertEqual(self.router.prs[0]["base"], "main")

        # comments posted (start + report)
        self.assertGreaterEqual(len(self.router.comments), 2)
        report = json.dumps(self.router.comments[-1], ensure_ascii=False)
        self.assertIn("pull/12", report)

        # Supabase persistence
        tasks = self.router.supabase_rows["tasks_log"]
        self.assertTrue(tasks, "tasks_log rows were not written")
        self.assertEqual(tasks[-1]["status"], "completed")
        self.assertEqual(tasks[-1]["pr_number"], 12)
        self.assertEqual(tasks[-1]["commit_sha"], COMMIT_SHA)
        memory = self.router.supabase_rows["system_memory"]
        self.assertTrue(memory, "system_memory rows were not written")
        self.assertTrue(any("health" in json.dumps(row).lower() for row in memory))
        self.assertTrue(self.router.supabase_rows["interactions"], "interactions were not recorded")
        self.assertEqual(agent.metrics["tasks"], 1)

    def test_dry_run_touches_nothing(self) -> None:
        cfg = abu.Config.from_env()
        agent = abu.Agent(cfg)
        agent.run(dry_run=True)
        self.assertEqual(self.router.commits, [], "dry run must not commit")
        self.assertEqual(self.router.prs, [], "dry run must not open PRs")

    def test_secrets_never_leak_into_comments(self) -> None:
        cfg = abu.Config.from_env()
        agent = abu.Agent(cfg)
        agent.run()
        blob = json.dumps(self.router.comments, ensure_ascii=False) + json.dumps(self.router.supabase_rows, ensure_ascii=False)
        for secret in (cfg.github_token, cfg.groq_api_key, cfg.gemini_api_key, cfg.supabase_key):
            self.assertNotIn(secret, blob, "a secret leaked into an artifact")
        log_text = Path(os.environ["ABU_LOG_FILE"]).read_text(encoding="utf-8")
        for secret in (cfg.github_token, cfg.groq_api_key, cfg.gemini_api_key, cfg.supabase_key):
            self.assertNotIn(secret, log_text, "a secret leaked into the log file")

    def test_comment_command_runs_and_enforces_permissions(self) -> None:
        """A `/abu …` comment from a writer triggers work; a reader is refused politely."""
        self._set_comment_event("/abu status", "hussein")
        agent = abu.Agent(abu.Config.from_env())
        self.assertEqual(agent.run(), 0)
        # the inline `status` directive ran straight against Supabase, with no LLM call
        self.assertTrue(any("/rest/v1/tasks_log" in call["url"] for call in self.router.calls))
        self.assertEqual(agent.metrics["tasks"], 1)
        # a pure lookup command must not touch the repository
        self.assertEqual(self.router.commits, [], "an info-only command must not commit")

        # now a reader-only user tries to command the agent
        self.router.comments.clear()
        self.router.commits.clear()
        self._set_comment_event("/abu deploy worker evil", "random-guy")
        agent = abu.Agent(abu.Config.from_env())
        self.assertEqual(agent.run(), 0)
        self.assertEqual(self.router.commits, [], "a reader must never trigger a commit")
        self.assertEqual(agent.metrics["tasks"], 0, "the task was refused before planning")
        refusal = json.dumps(self.router.comments, ensure_ascii=False)
        self.assertIn("صلاحية", refusal)

    def test_agent_ignores_its_own_comments(self) -> None:
        """No infinite loop: the agent must not treat its own comment as a command."""
        # posted by the PAT owner (a normal user account) but carrying our marker
        self._set_comment_event("✅ تم التنفيذ\n\n<!-- abu-ali:agent -->", "hussein")
        agent = abu.Agent(abu.Config.from_env())
        self.assertEqual(agent.run(), 0)
        self.assertEqual(agent.metrics["tasks"], 0, "the agent answered itself")
        self.assertEqual(self.router.commits, [])

    def test_scheduled_sweep_harvests_knowledge(self) -> None:
        os.environ["GITHUB_EVENT_NAME"] = "schedule"   # no issue, no comment: self-generated task
        os.environ.pop("GITHUB_EVENT_PATH", None)
        agent = abu.Agent(abu.Config.from_env())
        self.assertEqual(agent.run(), 0)
        self.assertEqual(agent.metrics["tasks"], 1)
        self.assertEqual(self.router.commits, [], "a sweep must not commit by default")
        memory = self.router.supabase_rows["system_memory"]
        self.assertTrue(any("sweep" in json.dumps(row).lower() for row in memory))
        os.environ["GITHUB_EVENT_PATH"] = str(self.event_file)

    def test_protected_path_is_refused(self) -> None:
        agent = abu.Agent(abu.Config.from_env())
        tool_result = agent.build_tool_schema()
        names = {tool["function"]["name"] for tool in tool_result}
        self.assertIn("delete_file", names)
        self.assertTrue(abu._is_protected_path(".github/workflows/abu_ali_agent.yml"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
