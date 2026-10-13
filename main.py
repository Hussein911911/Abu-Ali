#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================================
 Abu Ali — Autonomous AI DevOps Architect & Software Engineer
============================================================================
 Core agent engine. Runs inside GitHub Actions (or locally) and:

   1. Reads its trigger from the GitHub event payload
      (issues / issue_comment / workflow_dispatch / schedule / push).
   2. Loads long-term memory + task history from Supabase (PostgreSQL).
   3. Thinks with a dual-LLM brain (Groq = fast reasoning, Gemini = deep
      context & code generation) with automatic cross-provider fallback.
   4. Executes real engineering work on GitHub: branches, commits, files,
      Pull Requests, issue comments.
   5. Deploys Cloudflare Workers / Pages when edge code changed.
   6. Persists every action, decision and lesson back into Supabase so that
      NOTHING is ever lost between ephemeral CI runners.

 Design guarantees (hard rules encoded in code, not in prose):
   * NEVER delete data, tables, files or features. Only additive changes.
   * Zero placeholders: every path has real error handling + retries.
   * All secrets are read from the environment, never hard-coded, and are
     scrubbed from every log line before it is written.
   * If Supabase is unreachable, results are buffered in a repo outbox file
     (`.abu-ali/outbox.jsonl`) and flushed on the next successful run, so
     history survives runner death.

 Env var contract (GitHub Secrets mapped 1:1 in the workflow):
   SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
   GROQ_API_KEY, GEMINI_API_KEY,
   MY_GITHUB_TOKEN, CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN
   (+ optional CONTEXT7_API_KEY, SUPABASE_ACCESS_TOKEN for extra powers)

 CLI:
   python main.py                     # normal agent run (CI)
   python main.py --selftest          # offline validation, no network
   python main.py --dry-run           # plan + print, apply nothing
   python main.py --issue 42          # force work on a given issue
   python main.py --instruction "..."  # force a raw instruction
   python main.py --status            # print agent status and exit
============================================================================
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

# ---------------------------------------------------------------------------
# Optional dependencies — the agent degrades gracefully instead of exploding.
# The SDKs are preferred when installed (see requirements.txt); a pure-REST
# implementation exists for every single one of them as a safety net.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - environment dependent
    from dotenv import load_dotenv

    load_dotenv(override=False)
except Exception:  # pragma: no cover
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:  # type: ignore
        return False

try:  # pragma: no cover
    from supabase import create_client as _supabase_create_client  # type: ignore
except Exception:  # pragma: no cover
    _supabase_create_client = None  # type: ignore

try:  # pragma: no cover
    from github import Github as _PyGithub  # type: ignore
    from github.GithubException import GithubException as _PyGithubException  # type: ignore
except Exception:  # pragma: no cover
    _PyGithub = None  # type: ignore

    class _PyGithubException(Exception):  # type: ignore
        pass

try:  # pragma: no cover
    from groq import Groq as _GroqClient  # type: ignore
except Exception:  # pragma: no cover
    _GroqClient = None  # type: ignore

try:  # pragma: no cover
    import warnings as _warnings

    with _warnings.catch_warnings():
        # The classic SDK prints a loud deprecation banner on import; we support
        # both SDKs on purpose, so the banner is noise in the CI logs.
        _warnings.simplefilter("ignore", FutureWarning)
        import google.generativeai as _genai  # type: ignore  # classic SDK
except Exception:  # pragma: no cover
    _genai = None  # type: ignore

try:  # pragma: no cover
    from google import genai as _genai_modern  # type: ignore  # modern SDK (google-genai)
except Exception:  # pragma: no cover
    _genai_modern = None  # type: ignore


# ===========================================================================
#  0. CONSTANTS & IDENTITY
# ===========================================================================

AGENT_VERSION = "1.0.0"
AGENT_NAME = os.getenv("AGENT_NAME", "Abu Ali")
AGENT_HANDLE = os.getenv("AGENT_HANDLE", "abu-ali")
# Invisible provenance marker. Comments carrying it are recognised as the agent's
# own output even though GitHub attributes them to the token owner — without it a
# self-reply could bounce between the human and the agent forever.
AGENT_MARKER = "<!-- abu-ali:agent -->"
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "/abu")
LOG_FILE = os.getenv("ABU_LOG_FILE", "abu_ali_run.log")
OUTBOX_PATH = os.getenv("ABU_OUTBOX_PATH", ".abu-ali/outbox.jsonl")
INSTRUCTIONS_FILE = os.getenv("ABU_INSTRUCTIONS_FILE", "ABU_ALI.md")
STATE_DIR = ".abu-ali"

TASK_LABELS = [lbl.strip().lower() for lbl in os.getenv("TASK_LABELS", "task,devops").split(",") if lbl.strip()]
MANAGED_LABELS = [
    "task",
    "devops",
    "abu-ali",
    "agent-report",
    "agent-failure",
    "needs-human",
    "auto-fix",
]

# Hard safety ceilings. Raise them in the repo variables if you really want to,
# but they exist so a runaway model can never nuke your quotas or your repo.
MAX_FILES_PER_PR = int(os.getenv("MAX_FILES_PER_PR", "10"))
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", "180000"))
MAX_TOTAL_BYTES = int(os.getenv("MAX_TOTAL_BYTES", "600000"))
MAX_CONTEXT_CHARS = int(os.getenv("MAX_CONTEXT_CHARS", "60000"))
MAX_TOOL_TURNS = int(os.getenv("MAX_TOOL_TURNS", "14"))
MAX_COMMENT_CHARS = int(os.getenv("MAX_COMMENT_CHARS", "60000"))
MAX_ISSUES_PER_RUN = int(os.getenv("MAX_ISSUES_PER_RUN", "2"))
MAX_CONTEXT_FILES = int(os.getenv("MAX_CONTEXT_FILES", "8"))
PROTECTED_PATHS = [".git/", ".github/workflows/", ".env", "secrets", "node_modules/", ".venv/", "venv/"]
# The agent refuses to write `.github/workflows/**` unless the owner explicitly
# opts in (a prompt-injected issue must never be able to rewrite CI to steal
# secrets). Set the repository variable ABU_ALLOW_PROTECTED_WRITES=true to opt in.
ALLOW_PROTECTED_WRITES = os.getenv("ABU_ALLOW_PROTECTED_WRITES", "").strip().lower() in ("1", "true", "yes", "on")

# Paths the agent may write to for its own bookkeeping (never destructive).
ALLOWED_BOT_PATHS = (STATE_DIR + "/",)

LOG_FILE_HANDLE = None
_SECRET_VALUES: List[str] = []


# ===========================================================================
#  1. LOGGING (with automatic secret redaction)
# ===========================================================================

class SecretRedactingFilter(logging.Filter):
    """Removes any secret value that might leak into a log line or traceback."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = message
        for secret in _SECRET_VALUES:
            if secret and len(secret) >= 6 and secret in cleaned:
                cleaned = cleaned.replace(secret, "***REDACTED***")
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def register_secret(value: Optional[str]) -> None:
    """Remember a secret string so it can never be printed."""
    if value and len(value) >= 6 and value not in _SECRET_VALUES:
        _SECRET_VALUES.append(value)


class _ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, *args: Any, use_color: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.use_color = use_color and sys.stdout.isatty()

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if self.use_color:
            color = self.COLORS.get(record.levelname, "")
            if color:
                text = f"{color}{text}{self.RESET}"
        return text


def setup_logging(verbose: bool = True) -> logging.Logger:
    """Configure root logging: console + rotating-ish run log file for artifacts."""
    global LOG_FILE_HANDLE

    level = logging.DEBUG if os.getenv("ABU_DEBUG", "").lower() in ("1", "true", "yes") else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = "%(asctime)s | %(levelname)-7s | %(name)-14s | %(message)s"
    datefmt = "%Y-%m-%dT%H:%M:%SZ"

    if verbose:
        console = logging.StreamHandler(stream=sys.stdout)
        console.setFormatter(_ColorFormatter(fmt, datefmt=datefmt))
        console.addFilter(SecretRedactingFilter())
        root.addHandler(console)
    else:
        # Quiet mode still surfaces failures: an error must never be invisible
        # just because the chatty console handler is off.
        errors_only = logging.StreamHandler(stream=sys.stderr)
        errors_only.setLevel(logging.ERROR)
        errors_only.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        errors_only.addFilter(SecretRedactingFilter())
        root.addHandler(errors_only)

    try:
        # The log may live in a sub-directory (e.g. .abu-ali/run.log) that does not
        # exist yet on a fresh runner — create it instead of silently losing logs.
        log_parent = Path(LOG_FILE).expanduser().parent
        if str(log_parent) not in ("", "."):
            log_parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE_HANDLE = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
        LOG_FILE_HANDLE.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        LOG_FILE_HANDLE.addFilter(SecretRedactingFilter())
        root.addHandler(LOG_FILE_HANDLE)
    except Exception as exc:  # pragma: no cover - filesystem edge case
        print(f"[warn] could not open log file {LOG_FILE}: {exc}")

    # Silence noisy third-party loggers (they love dumping full URLs).
    for noisy in ("urllib3", "requests", "httpx", "httpcore", "github", "groq", "supabase"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log = logging.getLogger("abu-ali")
    log.debug("logging initialised (level=%s)", logging.getLevelName(level))
    return log


LOG = logging.getLogger("abu-ali")


# ===========================================================================
#  2. SMALL UTILITIES
# ===========================================================================

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def truncate(text: Optional[str], limit: int, note: str = "…[truncated]") -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(note))] + note


def slugify(value: str, max_len: int = 48) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^\w\s\-\u0600-\u06FF]+", "", value, flags=re.UNICODE)
    value = re.sub(r"[\s_]+", "-", value).strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return (value[:max_len] or "task").strip("-")


def sha1_hex(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", "ignore")).hexdigest()


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def b64encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def b64decode(data: str) -> str:
    try:
        return base64.b64decode(data or "", validate=False).decode("utf-8", "replace")
    except Exception:
        return ""


def json_dumps_safe(obj: Any, limit: int = 60000) -> str:
    try:
        return truncate(json.dumps(obj, ensure_ascii=False, default=str, indent=None), limit)
    except Exception:
        return truncate(repr(obj), limit)


def safe_json_loads(raw: Optional[str]) -> Optional[Any]:
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        pass
    # Some LLMs double-encode the payload as a JSON string inside a string.
    try:
        inner = json.loads(str(raw).strip())
        if isinstance(inner, str):
            return json.loads(inner)
    except Exception:
        return None
    return None


_JSON_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def extract_json_object(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Pull the first valid JSON object out of an LLM answer.

    Handles: raw JSON, ```json fenced blocks, prose before/after the object,
    trailing commas, and smart quotes. Returns None when nothing parses.
    """
    if not text:
        return None
    candidates: List[str] = []

    for match in _JSON_FENCE_RE.findall(text):
        candidates.append(match.strip())
    candidates.append(text.strip())

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        candidates.append(text[brace_start : brace_end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        for attempt in (candidate, _repair_json(candidate)):
            parsed = safe_json_loads(attempt)
            if isinstance(parsed, dict):
                return parsed
    return None


def _repair_json(text: str) -> str:
    """Best-effort repair of the classic LLM JSON mistakes."""
    repaired = text.strip()
    repaired = repaired.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = repaired.replace("True", "true").replace("False", "false").replace("None", "null")
    if repaired.startswith("'") and repaired.endswith("'"):
        repaired = '"' + repaired[1:-1].replace('"', '\\"') + '"'
    return repaired


def chunk_text(text: str, size: int) -> List[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def keyword_tokens(text: str, min_len: int = 4, limit: int = 40) -> List[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_\-]{3,}", (text or "").lower())
    stop = {
        "the", "and", "for", "with", "that", "this", "from", "have", "will", "your", "please",
        "make", "need", "want", "into", "when", "then", "they", "them", "some", "more", "less",
        "code", "file", "files", "issue", "task", "repo", "github", "should", "would", "could",
    }
    seen: List[str] = []
    for token in tokens:
        if len(token) < min_len or token in stop or token in seen:
            continue
        seen.append(token)
        if len(seen) >= limit:
            break
    return seen


# ===========================================================================
#  3. RETRY / RATE-LIMIT MACHINERY
# ===========================================================================

@dataclass
class RetryPolicy:
    attempts: int = 5
    base_delay: float = 1.6
    max_delay: float = 90.0
    jitter: float = 0.3

    def delay_for(self, attempt: int, retry_after: Optional[float] = None) -> float:
        if retry_after is not None and retry_after >= 0:
            base = min(retry_after, self.max_delay)
        else:
            base = min(self.base_delay * (2 ** max(0, attempt - 1)), self.max_delay)
        return base + random.uniform(0, max(0.0, base * self.jitter))


class RetryableError(Exception):
    """Transient failure — worth another attempt."""

    def __init__(self, message: str, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentError(Exception):
    """Hard failure — retrying will not help (auth, 404, validation…)."""


def retry_after_from_headers(headers: Optional[MappingLike]) -> Optional[float]:
    if not headers:
        return None
    for key in ("retry-after", "x-ratelimit-reset", "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        raw = headers.get(key) if hasattr(headers, "get") else None
        if not raw:
            continue
        try:
            value = float(str(raw).strip())
        except Exception:
            continue
        if key in ("retry-after",):
            return value
        # x-ratelimit-reset is an epoch timestamp OR a duration like "7.66s" / "1m2s"
        if key.endswith("requests") or key.endswith("tokens"):
            seconds = _duration_to_seconds(str(raw))
            if seconds is not None:
                return seconds
        if value > 10_000_000:  # epoch seconds
            return max(0.0, value - time.time())
        return value
    return None


def _duration_to_seconds(text: str) -> Optional[float]:
    text = text.strip()
    matches = re.findall(r"([0-9]*\.?[0-9]+)(ms|s|m|h)", text)
    if not matches:
        return None
    total = 0.0
    for amount, unit in matches:
        value = float(amount)
        total += {"ms": value / 1000.0, "s": value, "m": value * 60, "h": value * 3600}[unit]
    return total


MappingLike = Any  # requests.structures.CaseInsensitiveDict or plain dict


def call_with_retry(
    label: str,
    fn: Callable[[], Any],
    policy: Optional[RetryPolicy] = None,
    retry_on: Sequence[type] = (RetryableError,),
    logger: Optional[logging.Logger] = None,
) -> Any:
    """
    Execute `fn` with exponential backoff + jitter.

    Raises the last exception when every attempt failed. `PermanentError` and
    non-retryable exceptions bubble up immediately.
    """
    policy = policy or RetryPolicy()
    log = logger or LOG
    last_error: Optional[BaseException] = None

    for attempt in range(1, policy.attempts + 1):
        try:
            return fn()
        except PermanentError:
            raise
        except tuple(retry_on) as exc:  # type: ignore[arg-type]
            last_error = exc
            wait = policy.delay_for(attempt, getattr(exc, "retry_after", None))
            if attempt >= policy.attempts:
                log.error("[retry] %s failed after %d attempts: %s", label, attempt, exc)
                break
            log.warning(
                "[retry] %s attempt %d/%d failed (%s) — sleeping %.1fs",
                label, attempt, policy.attempts, type(exc).__name__, wait,
            )
            time.sleep(wait)
        except Exception as exc:  # non-retryable: log once and re-raise
            log.error("[retry] %s raised non-retryable %s: %s", label, type(exc).__name__, exc)
            raise

    if last_error is not None:
        raise last_error
    raise RetryableError(f"{label}: retries exhausted with no exception recorded")


def http_session() -> requests.Session:
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": f"AbuAli-Agent/{AGENT_VERSION} (+github-actions)"})
    return session


HTTP_SESSION = http_session()


def http_json(
    method: str,
    url: str,
    *,
    label: str,
    headers: Optional[Dict[str, str]] = None,
    json_body: Any = None,
    data: Any = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 90,
    policy: Optional[RetryPolicy] = None,
    ok_status: Sequence[int] = (200, 201, 202, 204),
    allow_404: bool = False,
    files: Any = None,
) -> Tuple[Optional[Any], int, Dict[str, str]]:
    """
    JSON-aware HTTP call with retries on 429 / 5xx / network errors.

    Returns (parsed_body_or_None, status_code, headers). On a non-ok status the
    body is returned as-is when it is JSON so callers can surface the error.
    """
    policy = policy or RetryPolicy()

    def _attempt() -> Tuple[Optional[Any], int, Dict[str, str]]:
        try:
            response = HTTP_SESSION.request(
                method.upper(),
                url,
                headers=headers,
                json=json_body,
                data=data,
                params=params,
                files=files,
                timeout=timeout,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            raise RetryableError(f"{label}: network error {exc}") from exc

        status = response.status_code
        resp_headers = {k.lower(): v for k, v in response.headers.items()}
        body: Optional[Any] = None
        text = response.text or ""
        if text.strip():
            try:
                body = response.json()
            except Exception:
                body = {"raw": truncate(text, 2000)}

        if status in ok_status:
            return body, status, resp_headers
        if status == 404 and allow_404:
            return body, status, resp_headers
        if status in (429, 500, 502, 503, 504, 522, 524):
            raise RetryableError(
                f"{label}: HTTP {status} {truncate(text, 300)}",
                retry_after=retry_after_from_headers(resp_headers),
            )
        if status == 403 and str(resp_headers.get("x-ratelimit-remaining", "")) == "0":
            raise RetryableError(
                f"{label}: primary rate limit exhausted",
                retry_after=retry_after_from_headers(resp_headers),
            )
        if status in (401, 403):
            raise PermanentError(f"{label}: HTTP {status} — check the token scopes/permissions: {truncate(text, 300)}")
        return body, status, resp_headers

    return call_with_retry(label, _attempt, policy=policy, retry_on=(RetryableError,), logger=LOG)


# ===========================================================================
#  4. CONFIGURATION
# ===========================================================================

@dataclass
class Config:
    # Secrets
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_access_token: str = ""
    groq_api_key: str = ""
    gemini_api_key: str = ""
    github_token: str = ""
    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""
    cloudflare_pages_project: str = ""
    context7_api_key: str = ""

    # Non-secret tuning
    repo_full_name: str = ""
    repo_owner: str = ""
    repo_name: str = ""
    default_branch: str = "main"
    workflow_file: str = "abu_ali_agent.yml"
    event_name: str = ""
    event_path: str = ""
    actor: str = ""
    run_id: str = ""
    run_number: str = ""
    server_url: str = "https://github.com"
    api_base: str = "https://api.github.com"
    groq_api_base: str = "https://api.groq.com/openai/v1"
    gemini_api_base: str = "https://generativelanguage.googleapis.com/v1beta"
    language: str = "ar"
    provider_order: List[str] = field(default_factory=lambda: ["groq", "gemini"])
    groq_models: List[str] = field(default_factory=lambda: ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"])
    gemini_models: List[str] = field(default_factory=lambda: ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"])
    auto_deploy: bool = True
    auto_merge: bool = False
    dry_run: bool = False
    merge_method: str = "squash"
    command_min_permission: str = "write"
    sweep_cooldown_hours: float = 6.0
    max_output_tokens: int = 8000
    temperature: float = 0.2
    mcp_servers: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def run_url(self) -> str:
        if self.repo_full_name and self.run_id:
            return f"{self.server_url}/{self.repo_full_name}/actions/runs/{self.run_id}"
        return ""

    @property
    def supabase_ready(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    @property
    def cloudflare_ready(self) -> bool:
        return bool(self.cloudflare_account_id and self.cloudflare_api_token)

    def describe(self) -> Dict[str, Any]:
        """Secret-free description used for logs, memory and comments."""
        return {
            "version": AGENT_VERSION,
            "repo": self.repo_full_name,
            "event": self.event_name,
            "actor": self.actor,
            "run_url": self.run_url,
            "supabase": self.supabase_ready,
            "groq": bool(self.groq_api_key),
            "gemini": bool(self.gemini_api_key),
            "github_token": bool(self.github_token),
            "cloudflare": self.cloudflare_ready,
            "mcp": sorted(self.mcp_servers.keys()),
            "dry_run": self.dry_run,
            "language": self.language,
        }

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()

        # ---- secrets -----------------------------------------------------
        cfg.supabase_url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
        cfg.supabase_key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY") or "").strip()
        cfg.supabase_access_token = (os.getenv("SUPABASE_ACCESS_TOKEN") or "").strip()
        cfg.groq_api_key = (os.getenv("GROQ_API_KEY") or "").strip()
        cfg.gemini_api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
        cfg.github_token = (os.getenv("MY_GITHUB_TOKEN") or os.getenv("GH_PAT") or "").strip()
        cfg.cloudflare_account_id = (os.getenv("CLOUDFLARE_ACCOUNT_ID") or "").strip()
        cfg.cloudflare_api_token = (os.getenv("CLOUDFLARE_API_TOKEN") or "").strip()
        cfg.cloudflare_pages_project = (os.getenv("CLOUDFLARE_PAGES_PROJECT") or "").strip()
        cfg.context7_api_key = (os.getenv("CONTEXT7_API_KEY") or "").strip()
        for secret in (
            cfg.supabase_key, cfg.supabase_access_token, cfg.groq_api_key, cfg.gemini_api_key,
            cfg.github_token, cfg.cloudflare_api_token, cfg.context7_api_key,
        ):
            register_secret(secret)

        # ---- repository / runtime context --------------------------------
        cfg.repo_full_name = os.getenv("GITHUB_REPOSITORY") or os.getenv("ABU_REPO") or ""
        if "/" in cfg.repo_full_name:
            cfg.repo_owner, cfg.repo_name = cfg.repo_full_name.split("/", 1)
        # The checkout's branch is authoritative (actions/checkout decides it),
        # otherwise fall back to the configured default.
        checkout_ref = os.getenv("GITHUB_REF_NAME") or ""
        if checkout_ref.startswith("refs/"):
            checkout_ref = checkout_ref.split("/", 2)[-1]
        cfg.default_branch = os.getenv("ABU_DEFAULT_BRANCH") or checkout_ref or "main"
        cfg.workflow_file = os.getenv("ABU_WORKFLOW_FILE", "abu_ali_agent.yml")
        cfg.event_name = os.getenv("GITHUB_EVENT_NAME", "")
        cfg.event_path = os.getenv("GITHUB_EVENT_PATH", "")
        cfg.actor = os.getenv("GITHUB_ACTOR", "")
        cfg.run_id = os.getenv("GITHUB_RUN_ID", "")
        cfg.run_number = os.getenv("GITHUB_RUN_NUMBER", "")
        cfg.server_url = (os.getenv("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
        cfg.api_base = (os.getenv("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        cfg.groq_api_base = (os.getenv("GROQ_API_BASE") or "https://api.groq.com/openai/v1").rstrip("/")
        cfg.gemini_api_base = (os.getenv("GEMINI_API_BASE") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")

        # ---- behaviour ----------------------------------------------------
        cfg.language = (os.getenv("AGENT_LANGUAGE") or "ar").strip().lower()
        order = (os.getenv("LLM_PROVIDER_ORDER") or "groq,gemini").replace(" ", "")
        cfg.provider_order = [p for p in order.split(",") if p in ("groq", "gemini")] or ["groq", "gemini"]
        cfg.groq_models = [m.strip() for m in (os.getenv("GROQ_MODELS") or ",".join(cfg.groq_models)).split(",") if m.strip()]
        cfg.gemini_models = [m.strip() for m in (os.getenv("GEMINI_MODELS") or ",".join(cfg.gemini_models)).split(",") if m.strip()]
        cfg.auto_deploy = _env_bool("AUTO_DEPLOY", True)
        cfg.auto_merge = _env_bool("AUTO_MERGE", False)
        cfg.dry_run = _env_bool("ABU_DRY_RUN", False) or _env_bool("DRY_RUN", False)
        cfg.merge_method = (os.getenv("MERGE_METHOD") or "squash").strip()
        cfg.command_min_permission = (os.getenv("COMMAND_MIN_PERMISSION") or "write").strip().lower()
        try:
            cfg.sweep_cooldown_hours = float(os.getenv("SWEEP_COOLDOWN_HOURS", "6"))
        except Exception:
            cfg.sweep_cooldown_hours = 6.0
        try:
            cfg.max_output_tokens = int(os.getenv("MAX_OUTPUT_TOKENS", "8000"))
            cfg.temperature = float(os.getenv("LLM_TEMPERATURE", "0.2"))
        except Exception:
            pass
        cfg.mcp_servers = _load_mcp_servers(cfg)
        return cfg


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


def _load_mcp_servers(cfg: "Config") -> Dict[str, Dict[str, Any]]:
    """
    MCP (Model Context Protocol) server catalogue.

    Override/extend with the `MCP_SERVERS` env var (JSON):
      {"context7": {"url": "https://mcp.context7.com/mcp", "transport": "http",
                    "apiKeyEnv": "CONTEXT7_API_KEY", "tools": ["resolve-library-id", "get-library-docs"]}}
    Sensible defaults for GitHub, Supabase and Context7 are always present.
    """
    defaults: Dict[str, Dict[str, Any]] = {
        "github": {
            "url": os.getenv("MCP_GITHUB_URL", "https://api.githubcopilot.com/mcp/"),
            "transport": "http",
            "apiKeyEnv": "MY_GITHUB_TOKEN",
            "enabled": bool(cfg.github_token) and _env_bool("MCP_ENABLE_GITHUB", False),
            "tools": ["search_repositories", "get_file_contents", "list_issues"],
        },
        "supabase": {
            "url": os.getenv("MCP_SUPABASE_URL", "https://mcp.supabase.com/mcp"),
            "transport": "http",
            "apiKeyEnv": "SUPABASE_ACCESS_TOKEN",
            "enabled": bool(cfg.supabase_access_token) and _env_bool("MCP_ENABLE_SUPABASE", False),
            "tools": ["list_tables", "execute_sql", "apply_migration"],
        },
        "context7": {
            "url": os.getenv("MCP_CONTEXT7_URL", "https://mcp.context7.com/mcp"),
            "transport": "http",
            "apiKeyEnv": "CONTEXT7_API_KEY",
            "enabled": _env_bool("MCP_ENABLE_CONTEXT7", True),
            "tools": ["resolve-library-id", "get-library-docs"],
        },
    }
    raw = os.getenv("MCP_SERVERS")
    if raw:
        try:
            overrides = json.loads(raw)
            if isinstance(overrides, dict):
                for key, value in overrides.items():
                    if isinstance(value, dict) and key in defaults:
                        defaults[key].update(value)
                    elif isinstance(value, dict):
                        defaults[key] = value
        except Exception as exc:
            LOG.warning("MCP_SERVERS is not valid JSON (%s) — using defaults", exc)
    return {k: v for k, v in defaults.items() if v.get("enabled")}


# ===========================================================================
#  5. DUAL-LLM BRAIN — Groq (fast) + Gemini (deep) with cross-fallback
# ===========================================================================

@dataclass
class LLMAnswer:
    text: str
    provider: str
    model: str
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.text and self.text.strip())


def looks_like_rate_limit(payload: Any, status: int = 0) -> bool:
    if status in (429, 402, 503):
        return True
    text = json_dumps_safe(payload, 4000).lower()
    return any(needle in text for needle in ("rate limit", "quota", "resource_exhausted", "too many requests", "overloaded"))


class LLMEngine:
    """
    Dual-LLM router.

    * Groq  → latency-critical reasoning and planning (`llama-3.3-70b-versatile`).
    * Gemini→ long-context reasoning and production code generation.

    Every call walks the provider order, then the model list of each provider,
    and only raises when *all* of them failed. Rate limits are retried with the
    provider's own `retry-after` hint when available.
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = LOG
        self._groq_client = None
        self._gemini_modern = None
        self._gemini_configured = False
        self.usage: List[Dict[str, Any]] = []
        self._prepare_clients()

    # -- client preparation ------------------------------------------------
    def _prepare_clients(self) -> None:
        if self.cfg.groq_api_key and _GroqClient is not None:
            try:
                self._groq_client = _GroqClient(api_key=self.cfg.groq_api_key)
                self.log.info("Groq SDK client ready (%s)", ", ".join(self.cfg.groq_models))
            except Exception as exc:
                self.log.warning("Groq SDK init failed (%s) — will use REST", exc)
                self._groq_client = None
        if self.cfg.gemini_api_key:
            if _genai_modern is not None:
                try:
                    self._gemini_modern = _genai_modern.Client(api_key=self.cfg.gemini_api_key)
                    self._gemini_configured = True
                    self.log.info("Gemini SDK ready (google-genai, %s)", ", ".join(self.cfg.gemini_models))
                except Exception as exc:
                    self.log.warning("google-genai init failed (%s)", exc)
                    self._gemini_modern = None
            if not self._gemini_configured and _genai is not None:
                try:
                    _genai.configure(api_key=self.cfg.gemini_api_key)
                    self._gemini_configured = True
                    self.log.info("Gemini SDK configured (google-generativeai, %s)", ", ".join(self.cfg.gemini_models))
                except Exception as exc:
                    self.log.warning("Gemini SDK init failed (%s) — will use REST", exc)
                    self._gemini_configured = False

    @property
    def available(self) -> bool:
        return bool(self.cfg.groq_api_key or self.cfg.gemini_api_key)

    def describe_brain(self) -> str:
        parts = []
        if self.cfg.groq_api_key:
            parts.append(f"Groq({self.cfg.groq_models[0]})")
        if self.cfg.gemini_api_key:
            parts.append(f"Gemini({self.cfg.gemini_models[0]})")
        return " + ".join(parts) or "no-llm"

    # -- public API --------------------------------------------------------
    def ask(
        self,
        prompt: str,
        *,
        system: str = "",
        prefer: Optional[str] = None,
        json_mode: bool = False,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        label: str = "llm",
        validate: Optional[Callable[[LLMAnswer], bool]] = None,
    ) -> LLMAnswer:
        """
        Ask the brain. `prefer` ('groq'|'gemini') moves that provider first.

        `validate` lets the caller reject an answer that arrived successfully but
        is unusable (e.g. prose when JSON was demanded). A rejected answer is
        treated as a failure and the router moves on to the next provider/model.

        Raises RuntimeError only when every provider/model combination failed.
        """
        order = list(self.cfg.provider_order)
        if prefer and prefer in order:
            order.remove(prefer)
            order.insert(0, prefer)

        errors: List[str] = []
        for provider in order:
            api_key = self.cfg.groq_api_key if provider == "groq" else self.cfg.gemini_api_key
            if not api_key:
                errors.append(f"{provider}: no api key configured")
                continue
            models = self.cfg.groq_models if provider == "groq" else self.cfg.gemini_models
            for model in models:
                try:
                    answer = self._call_provider(
                        provider, model, prompt, system=system, json_mode=json_mode,
                        temperature=temperature, max_tokens=max_tokens, label=label,
                    )
                    if answer.ok and (validate is None or validate(answer)):
                        self._record(answer, label)
                        return answer
                    if not answer.ok:
                        errors.append(f"{provider}/{model}: empty response")
                    else:
                        self.log.warning("[%s] %s/%s answered but the answer was rejected by the validator", label, provider, model)
                        errors.append(f"{provider}/{model}: answer rejected by the validator")
                except PermanentError as exc:
                    self.log.warning("[%s] %s/%s permanently failed: %s", label, provider, model, exc)
                    errors.append(f"{provider}/{model}: {exc}")
                    break  # auth/authorization problem → skip remaining models of this provider
                except Exception as exc:
                    self.log.warning("[%s] %s/%s failed: %s", label, provider, model, exc)
                    errors.append(f"{provider}/{model}: {exc}")
                    continue
        raise RuntimeError(f"{label}: all LLM routes failed → " + " | ".join(errors[:8]))

    def plan_json(
        self,
        prompt: str,
        *,
        system: str = "",
        prefer: Optional[str] = None,
        label: str = "plan",
        max_attempts: int = 3,
    ) -> Dict[str, Any]:
        """
        Ask for JSON and repair-feedback until it parses (or attempts run out).

        The model receives its own broken output back with the parse error, which
        reliably fixes the "prose + trailing comma" class of failures.
        """
        attempt_prompt = prompt
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            answer = self.ask(
                attempt_prompt, system=system, prefer=prefer, json_mode=True,
                label=f"{label}#{attempt}",
                validate=lambda candidate: extract_json_object(candidate.text) is not None,
            )
            parsed = extract_json_object(answer.text)
            if parsed is not None:
                parsed.setdefault("_meta", {})
                if isinstance(parsed["_meta"], dict):
                    parsed["_meta"].update({
                        "provider": answer.provider,
                        "model": answer.model,
                        "latency_ms": answer.latency_ms,
                        "attempt": attempt,
                    })
                return parsed
            last_error = "the response was not valid JSON"
            self.log.warning("[%s] attempt %d produced unparsable JSON — asking the model to fix it", label, attempt)
            attempt_prompt = (
                f"{prompt}\n\n"
                f"### CORRECTION REQUIRED\n"
                f"Your previous answer was rejected because {last_error}. "
                f"Reply with ONE valid JSON object only. No markdown fences, no commentary.\n"
                f"Broken answer (first 1500 chars):\n{truncate(answer.text, 1500)}"
            )
        raise RuntimeError(f"{label}: could not obtain valid JSON ({last_error})")

    # -- providers ---------------------------------------------------------
    def _call_provider(
        self,
        provider: str,
        model: str,
        prompt: str,
        *,
        system: str,
        json_mode: bool,
        temperature: Optional[float],
        max_tokens: Optional[int],
        label: str,
    ) -> LLMAnswer:
        started = time.time()
        temp = self.cfg.temperature if temperature is None else temperature
        tokens = self.cfg.max_output_tokens if max_tokens is None else max_tokens

        if provider == "groq":
            answer = self._groq_chat(model, prompt, system=system, json_mode=json_mode, temperature=temp, max_tokens=tokens)
        elif provider == "gemini":
            answer = self._gemini_generate(model, prompt, system=system, json_mode=json_mode, temperature=temp, max_tokens=tokens)
        else:
            raise PermanentError(f"unknown provider {provider}")

        answer.latency_ms = int((time.time() - started) * 1000)
        self.log.info(
            "[%s] %s/%s answered in %.1fs (%d chars)",
            label, provider, model, answer.latency_ms / 1000.0, len(answer.text or ""),
        )
        return answer

    # ---- Groq ------------------------------------------------------------
    def _groq_chat(self, model: str, prompt: str, *, system: str, json_mode: bool,
                   temperature: float, max_tokens: int) -> LLMAnswer:
        messages: List[Dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        if self._groq_client is not None:
            def _call() -> LLMAnswer:
                try:
                    kwargs: Dict[str, Any] = {
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }
                    if json_mode:
                        kwargs["response_format"] = {"type": "json_object"}
                    completion = self._groq_client.chat.completions.create(**kwargs)
                    text = (completion.choices[0].message.content or "") if completion.choices else ""
                    usage = getattr(completion, "usage", None)
                    return LLMAnswer(
                        text=text,
                        provider="groq",
                        model=model,
                        tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0),
                        tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
                    )
                except Exception as exc:
                    raise _classify_provider_error(exc, "groq") from exc

            try:
                return call_with_retry(f"groq/{model}", _call, RetryPolicy(attempts=4, base_delay=2.0, max_delay=75.0))
            except Exception as exc:
                self.log.info("Groq SDK path failed for %s (%s) — trying REST", model, exc)

        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        def _rest() -> LLMAnswer:
            body, status, _ = http_json(
                "POST",
                f"{self.cfg.groq_api_base}/chat/completions",
                label=f"groq-rest/{model}",
                headers={"Authorization": f"Bearer {self.cfg.groq_api_key}", "Content-Type": "application/json"},
                json_body=payload,
                policy=RetryPolicy(attempts=4, base_delay=2.0, max_delay=75.0),
            )
            if status == 429 or (body and looks_like_rate_limit(body, status)):
                raise RetryableError(f"groq rate limited: {json_dumps_safe(body, 400)}")
            if status >= 400 or not body:
                raise PermanentError(f"groq REST HTTP {status}: {json_dumps_safe(body, 400)}")
            try:
                choice = (body.get("choices") or [{}])[0]
                text = (choice.get("message") or {}).get("content", "")
                usage = body.get("usage") or {}
            except Exception as exc:
                raise PermanentError(f"groq REST malformed response: {exc}") from exc
            return LLMAnswer(
                text=text or "",
                provider="groq",
                model=body.get("model", model),
                tokens_in=int(usage.get("prompt_tokens", 0) or 0),
                tokens_out=int(usage.get("completion_tokens", 0) or 0),
            )

        return call_with_retry(f"groq-rest/{model}", _rest, RetryPolicy(attempts=4, base_delay=2.0, max_delay=75.0))

    # ---- Gemini ----------------------------------------------------------
    def _gemini_generate(self, model: str, prompt: str, *, system: str, json_mode: bool,
                         temperature: float, max_tokens: int) -> LLMAnswer:
        if self._gemini_modern is not None:
            def _modern_call() -> LLMAnswer:
                try:
                    response = self._gemini_modern.models.generate_content(
                        model=model,
                        contents=prompt,
                        config={
                            "temperature": temperature,
                            "max_output_tokens": max_tokens,
                            "system_instruction": system or None,
                            **({"response_mime_type": "application/json"} if json_mode else {}),
                        },
                    )
                    usage = getattr(response, "usage_metadata", None)
                    return LLMAnswer(
                        text=(getattr(response, "text", "") or ""),
                        provider="gemini",
                        model=model,
                        tokens_in=int(getattr(usage, "prompt_token_count", 0) or 0),
                        tokens_out=int(getattr(usage, "candidates_token_count", 0) or 0),
                    )
                except Exception as exc:
                    raise _classify_provider_error(exc, "gemini") from exc

            try:
                return call_with_retry(f"gemini-genai/{model}", _modern_call,
                                       RetryPolicy(attempts=4, base_delay=3.0, max_delay=90.0))
            except Exception as exc:
                self.log.info("google-genai path failed for %s (%s) — trying the legacy SDK/REST", model, exc)

        if self._gemini_configured and _genai is not None:
            def _call() -> LLMAnswer:
                try:
                    gen_model = _genai.GenerativeModel(model_name=model, system_instruction=system or None)
                    generation_config = _genai.types.GenerationConfig(
                        temperature=temperature,
                        max_output_tokens=max_tokens,
                        response_mime_type="application/json" if json_mode else "text/plain",
                    )
                    response = gen_model.generate_content(prompt, generation_config=generation_config)
                    text = _extract_gemini_text(response)
                    usage = getattr(response, "usage_metadata", None)
                    return LLMAnswer(
                        text=text,
                        provider="gemini",
                        model=model,
                        tokens_in=int(getattr(usage, "prompt_token_count", 0) or 0),
                        tokens_out=int(getattr(usage, "candidates_token_count", 0) or 0),
                    )
                except Exception as exc:
                    raise _classify_provider_error(exc, "gemini") from exc

            try:
                return call_with_retry(f"gemini/{model}", _call, RetryPolicy(attempts=4, base_delay=3.0, max_delay=90.0))
            except Exception as exc:
                self.log.info("Gemini SDK path failed for %s (%s) — trying REST", model, exc)

        def _rest() -> LLMAnswer:
            url = f"{self.cfg.gemini_api_base}/models/{model}:generateContent"
            payload: Dict[str, Any] = {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                    **({"responseMimeType": "application/json"} if json_mode else {}),
                },
            }
            if system:
                payload["systemInstruction"] = {"parts": [{"text": system}]}

            body, status, _ = http_json(
                "POST",
                url,
                label=f"gemini-rest/{model}",
                headers={"x-goog-api-key": self.cfg.gemini_api_key, "Content-Type": "application/json"},
                json_body=payload,
                policy=RetryPolicy(attempts=4, base_delay=3.0, max_delay=90.0),
                ok_status=(200,),
            )
            if status == 429 or (body and looks_like_rate_limit(body, status)):
                raise RetryableError(f"gemini rate limited: {json_dumps_safe(body, 400)}")
            if status in (400, 401, 403) and body:
                raise PermanentError(f"gemini REST HTTP {status}: {json_dumps_safe(body, 400)}")
            if status >= 400 or not body:
                raise PermanentError(f"gemini REST HTTP {status}: {json_dumps_safe(body, 400)}")
            text = _extract_gemini_text_dict(body)
            usage = (body.get("usageMetadata") or {})
            return LLMAnswer(
                text=text,
                provider="gemini",
                model=body.get("modelVersion", model),
                tokens_in=int(usage.get("promptTokenCount", 0) or 0),
                tokens_out=int(usage.get("candidatesTokenCount", 0) or 0),
            )

        return call_with_retry(f"gemini-rest/{model}", _rest, RetryPolicy(attempts=4, base_delay=3.0, max_delay=90.0))

    # -- bookkeeping -------------------------------------------------------
    def _record(self, answer: LLMAnswer, label: str) -> None:
        self.usage.append({
            "label": label,
            "provider": answer.provider,
            "model": answer.model,
            "latency_ms": answer.latency_ms,
            "tokens_in": answer.tokens_in,
            "tokens_out": answer.tokens_out,
        })

    def usage_summary(self) -> Dict[str, Any]:
        return {
            "calls": len(self.usage),
            "total_latency_ms": sum(u["latency_ms"] for u in self.usage),
            "tokens_in": sum(u["tokens_in"] for u in self.usage),
            "tokens_out": sum(u["tokens_out"] for u in self.usage),
            "providers": sorted({u["provider"] for u in self.usage}),
            "detail": self.usage[-12:],
        }


def _classify_provider_error(exc: BaseException, provider: str) -> BaseException:
    """Map SDK exceptions onto our retry taxonomy."""
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(needle in text for needle in ("429", "rate limit", "rate_limit", "quota", "resource_exhausted", "too many requests", "overloaded", "503", "unavailable")):
        return RetryableError(f"{provider} transient error: {truncate(str(exc), 300)}", retry_after=_retry_hint_from_text(str(exc)))
    if any(needle in text for needle in ("401", "403", "invalid api key", "permission", "unauthenticated", "api_key_invalid")):
        return PermanentError(f"{provider} auth error: {truncate(str(exc), 300)}")
    if any(needle in text for needle in ("timeout", "connection", "timed out", "ssl", "broken pipe")):
        return RetryableError(f"{provider} network error: {truncate(str(exc), 300)}")
    return exc


def _retry_hint_from_text(text: str) -> Optional[float]:
    match = re.search(r"retry[_ \-]?after[\"':\s]+([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except Exception:
            return None
    return None


def _extract_gemini_text(response: Any) -> str:
    try:
        text = getattr(response, "text", None)
        if text:
            return text
    except Exception:
        pass
    try:
        candidates = getattr(response, "candidates", None) or []
        for candidate in candidates:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) or []
            joined = "".join(getattr(part, "text", "") or "" for part in parts)
            if joined.strip():
                return joined
    except Exception:
        pass
    return ""


def _extract_gemini_text_dict(body: Dict[str, Any]) -> str:
    try:
        for candidate in body.get("candidates") or []:
            parts = ((candidate.get("content") or {}).get("parts")) or []
            joined = "".join(part.get("text", "") or "" for part in parts)
            if joined.strip():
                return joined
        if body.get("promptFeedback", {}).get("blockReason"):
            raise PermanentError(f"gemini blocked the prompt: {body['promptFeedback']['blockReason']}")
    except PermanentError:
        raise
    except Exception:
        pass
    return ""


# ===========================================================================
#  6. SUPABASE — STATE, LONG-TERM MEMORY, TASK LOG, OUTBOX SAFETY NET
# ===========================================================================

SCHEMA_SQL = """
-- Abu Ali agent schema (idempotent, additive only — nothing is ever dropped)
create extension if not exists pgcrypto;

create table if not exists public.tasks_log (
  id             bigint generated by default as identity primary key,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  repo           text        not null default '',
  issue_number   integer,
  issue_title    text,
  trigger        text,
  actor          text,
  status         text        not null default 'queued',
  stage          text,
  summary        text,
  details        jsonb       not null default '{}'::jsonb,
  llm_provider   text,
  llm_model      text,
  branch         text,
  pr_number      integer,
  pr_url         text,
  commit_sha     text,
  duration_ms    integer,
  error          text,
  run_id         text,
  run_url        text
);
create index if not exists tasks_log_repo_issue_idx  on public.tasks_log (repo, issue_number);
create index if not exists tasks_log_status_idx      on public.tasks_log (status, created_at desc);
create index if not exists tasks_log_created_idx     on public.tasks_log (created_at desc);

create table if not exists public.system_memory (
  id             bigint generated by default as identity primary key,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  repo           text        not null default '',
  kind           text        not null default 'fact',
  key            text        not null default '',
  content        text        not null,
  source         text,
  tags           text[]      not null default '{}',
  importance     integer     not null default 5,
  hits           integer     not null default 0,
  metadata       jsonb       not null default '{}'::jsonb,
  unique (repo, key)
);
create index if not exists system_memory_repo_kind_idx on public.system_memory (repo, kind, importance desc);
create index if not exists system_memory_tags_idx      on public.system_memory using gin (tags);

create table if not exists public.interactions (
  id             bigint generated by default as identity primary key,
  created_at     timestamptz not null default now(),
  repo           text        not null default '',
  issue_number   integer,
  author         text,
  role           text        not null default 'user',
  body           text        not null default '',
  run_id         text,
  metadata       jsonb       not null default '{}'::jsonb
);
create index if not exists interactions_repo_issue_idx on public.interactions (repo, issue_number, created_at);

create table if not exists public.deployments (
  id             bigint generated by default as identity primary key,
  created_at     timestamptz not null default now(),
  repo           text        not null default '',
  provider       text        not null default 'cloudflare',
  target_type    text,
  target_name    text,
  status         text        not null default 'pending',
  url            text,
  commit_sha     text,
  issue_number   integer,
  details        jsonb       not null default '{}'::jsonb,
  error          text
);

create or replace function public.abu_touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end $$;

do $$
declare t text;
begin
  foreach t in array array['tasks_log','system_memory'] loop
    execute format('drop trigger if exists %I_touch on public.%I', t, t);
    execute format('create trigger %I_touch before update on public.%I for each row execute function public.abu_touch_updated_at()', t, t);
  end loop;
end $$;

-- Optional helper so the agent can self-heal a missing table.
-- Security definer + RLS disabled: this database is the agent's private memory.
create or replace function public.abu_exec_sql(query text)
returns jsonb language plpgsql security definer as $$
begin
  execute query;
  return jsonb_build_object('ok', true);
exception when others then
  return jsonb_build_object('ok', false, 'error', sqlerrm);
end $$;
""".strip()


class MemoryStore:
    """
    Supabase persistence layer (PostgreSQL + REST Data API, RLS disabled).

    Failure model: if Supabase cannot be reached, every write is appended to
    `.abu-ali/outbox.jsonl` inside the repository (via the GitHub API) so that
    history is *never* lost. The next successful run flushes the outbox.
    """

    TABLES = ("tasks_log", "system_memory", "interactions", "deployments")

    def __init__(self, cfg: Config, github: Optional["GitHubClient"] = None) -> None:
        self.cfg = cfg
        self.github = github
        self.log = LOG
        self.client = None
        self.schema_ready = False
        self.pending_ddl = ""
        self._warned = False
        if cfg.supabase_ready:
            self._connect()
        else:
            self.log.warning("Supabase is not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing) — running in memory-only mode")

    # -- connection --------------------------------------------------------
    def _connect(self) -> None:
        if _supabase_create_client is not None:
            try:
                import warnings as _warnings

                with _warnings.catch_warnings():
                    # supabase-py 2.x emits DeprecationWarnings about `verify`;
                    # keep the CI logs readable, the behaviour is unaffected.
                    _warnings.simplefilter("ignore", DeprecationWarning)
                    self.client = _supabase_create_client(self.cfg.supabase_url, self.cfg.supabase_key)
                self.log.info("Supabase SDK client initialised for %s", self.cfg.supabase_url)
                return
            except Exception as exc:
                self.log.warning("Supabase SDK client failed (%s) — falling back to REST", exc)
        self.client = None
        self.log.info("Using Supabase REST API fallback against %s", self.cfg.supabase_url)

    @property
    def _rest_headers(self) -> Dict[str, str]:
        return {
            "apikey": self.cfg.supabase_key,
            "Authorization": f"Bearer {self.cfg.supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    # -- schema ------------------------------------------------------------
    def ensure_schema(self) -> bool:
        """
        Check that the agent tables exist; create them if the DB exposes the
        `abu_exec_sql` / `exec_sql` helper. Never touches existing tables.
        """
        if not self.cfg.supabase_ready:
            return False
        missing = [t for t in self.TABLES if not self._table_exists(t)]
        if not missing:
            self.schema_ready = True
            self.log.info("Supabase schema verified — all %d agent tables present", len(self.TABLES))
            return True

        self.log.warning("Supabase tables missing: %s — attempting self-heal", ", ".join(missing))
        created = self._try_exec_ddl(SCHEMA_SQL)
        if created:
            still_missing = [t for t in self.TABLES if not self._table_exists(t)]
            if not still_missing:
                self.schema_ready = True
                self.log.info("Supabase schema created by the agent ✔")
                return True
            self.pending_ddl = _ddl_for(still_missing)
            self.log.error("Self-heal incomplete. Remaining missing tables: %s", ", ".join(still_missing))
            return False

        self.pending_ddl = _ddl_for(missing)
        self.log.error(
            "Could not create tables automatically. Run db/schema.sql once in the Supabase SQL editor "
            "(or create the public.abu_exec_sql helper). Missing: %s",
            ", ".join(missing),
        )
        return False

    def _table_exists(self, table: str) -> bool:
        try:
            body, status, _ = http_json(
                "GET",
                f"{self.cfg.supabase_url}/rest/v1/{table}",
                label=f"supabase probe {table}",
                headers=self._rest_headers,
                params={"select": "id", "limit": "1"},
                ok_status=(200, 206, 416),
                allow_404=True,
                policy=RetryPolicy(attempts=3, base_delay=1.5),
            )
            return status in (200, 206, 416)
        except Exception as exc:
            self.log.warning("Supabase table probe for %s failed: %s", table, exc)
            return False

    def _try_exec_ddl(self, ddl: str) -> bool:
        for fn in ("abu_exec_sql", "exec_sql"):
            try:
                body, status, _ = http_json(
                    "POST",
                    f"{self.cfg.supabase_url}/rest/v1/rpc/{fn}",
                    label=f"supabase rpc {fn}",
                    headers=self._rest_headers,
                    json_body={"query": ddl},
                    ok_status=(200, 201, 204),
                    allow_404=True,
                    policy=RetryPolicy(attempts=2, base_delay=1.0),
                )
                if status in (200, 201, 204):
                    self.log.info("DDL executed through rpc/%s ✔", fn)
                    return True
            except Exception as exc:
                self.log.debug("rpc/%s DDL attempt failed: %s", fn, exc)
        # Optional: Management API (works if a personal access token is provided)
        if self.cfg.supabase_access_token:
            project_ref = _supabase_project_ref(self.cfg.supabase_url)
            if project_ref:
                try:
                    body, status, _ = http_json(
                        "POST",
                        f"https://api.supabase.com/v1/projects/{project_ref}/database/query",
                        label="supabase management api",
                        headers={
                            "Authorization": f"Bearer {self.cfg.supabase_access_token}",
                            "Content-Type": "application/json",
                        },
                        json_body={"query": ddl},
                        ok_status=(200, 201, 204),
                        policy=RetryPolicy(attempts=2, base_delay=1.5),
                    )
                    if status in (200, 201, 204):
                        self.log.info("DDL executed through the Supabase Management API ✔")
                        return True
                    self.log.warning("Management API rejected the DDL: %s", json_dumps_safe(body, 300))
                except Exception as exc:
                    self.log.warning("Management API DDL attempt failed: %s", exc)
        return False

    # -- generic insert ----------------------------------------------------
    def _insert(self, table: str, rows: Any, *, on_conflict: Optional[str] = None) -> bool:
        if not self.cfg.supabase_ready:
            return False
        rows = rows if isinstance(rows, list) else [rows]
        if not rows:
            return True
        prefer = "return=representation,resolution=merge-duplicates" if on_conflict else "return=representation"

        if self.client is not None:
            try:
                query = self.client.table(table)
                if on_conflict:
                    response = query.upsert(rows, on_conflict=on_conflict).execute()
                else:
                    response = query.insert(rows).execute()
                _ = getattr(response, "data", None)
                return True
            except Exception as exc:
                self.log.warning("Supabase SDK insert into %s failed (%s) — retrying over REST", table, exc)

        try:
            params = {"on_conflict": on_conflict} if on_conflict else None
            headers = dict(self._rest_headers)
            headers["Prefer"] = prefer
            _, status, _ = http_json(
                "POST",
                f"{self.cfg.supabase_url}/rest/v1/{table}",
                label=f"supabase insert {table}",
                headers=headers,
                params=params,
                json_body=rows,
                ok_status=(200, 201, 204),
                policy=RetryPolicy(attempts=3, base_delay=1.5, max_delay=30.0),
            )
            return status in (200, 201, 204)
        except Exception as exc:
            self.log.warning("Supabase REST insert into %s failed: %s", table, exc)
            self._buffer(table, rows, on_conflict)
            return False

    def _update(self, table: str, match: Dict[str, Any], fields: Dict[str, Any]) -> bool:
        if not self.cfg.supabase_ready or not match:
            return False
        if self.client is not None:
            try:
                query = self.client.table(table).update(fields)
                for key, value in match.items():
                    query = query.eq(key, value)
                query.execute()
                return True
            except Exception as exc:
                self.log.warning("Supabase SDK update on %s failed (%s) — retrying over REST", table, exc)

        try:
            params = {key: f"eq.{value}" for key, value in match.items()}
            headers = dict(self._rest_headers)
            _, status, _ = http_json(
                "PATCH",
                f"{self.cfg.supabase_url}/rest/v1/{table}",
                label=f"supabase update {table}",
                headers=headers,
                params=params,
                json_body=fields,
                ok_status=(200, 204),
                policy=RetryPolicy(attempts=3, base_delay=1.5),
            )
            return status in (200, 204)
        except Exception as exc:
            self.log.warning("Supabase REST update on %s failed: %s", table, exc)
            return False

    def _select(self, table: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self.cfg.supabase_ready:
            return []
        params = dict(params or {})
        if self.client is not None:
            try:
                query = self.client.table(table).select(params.get("select", "*"))
                order_raw = str(params.get("order") or "")
                ordering = [part.strip() for part in order_raw.split(",") if part.strip()]
                for key, value in params.items():
                    if key in ("select", "order", "limit"):
                        continue
                    if isinstance(value, str) and value.startswith("eq."):
                        query = query.eq(key, value[3:])
                    elif isinstance(value, str) and value.startswith("ilike."):
                        query = query.ilike(key, value[6:])
                for part in ordering:
                    if "." in part:
                        column, _, direction = part.rpartition(".")
                    else:
                        column, direction = part, "asc"
                    query = query.order(column, desc=direction.strip().lower().startswith("desc"))
                query = query.limit(int(params.get("limit", 20)))
                response = query.execute()
                data = getattr(response, "data", None) or []
                if isinstance(data, list):
                    return [row for row in data if isinstance(row, dict)]
            except Exception as exc:
                self.log.debug("Supabase SDK select on %s failed (%s) — REST fallback", table, exc)

        try:
            body, status, _ = http_json(
                "GET",
                f"{self.cfg.supabase_url}/rest/v1/{table}",
                label=f"supabase select {table}",
                headers=self._rest_headers,
                params=params,
                ok_status=(200, 206),
                policy=RetryPolicy(attempts=3, base_delay=1.0),
            )
            if isinstance(body, list):
                return [row for row in body if isinstance(row, dict)]
        except Exception as exc:
            self.log.warning("Supabase REST select on %s failed: %s", table, exc)
        return []

    # -- outbox safety net -------------------------------------------------
    def _buffer(self, table: str, rows: Any, on_conflict: Optional[str]) -> None:
        """Persist failed writes in the repo so they survive the CI runner."""
        try:
            payloads = [
                {"_t": "insert", "table": table, "rows": rows, "on_conflict": on_conflict, "at": now_iso()}
            ]
            blob = "\n".join(json.dumps(item, ensure_ascii=False, default=str) for item in payloads) + "\n"
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(OUTBOX_PATH, "a", encoding="utf-8") as handle:
                handle.write(blob)
            if "GITHUB_ACTIONS" in os.environ:
                self.flush_outbox()
        except Exception as exc:
            self.log.error("Outbox buffering failed: %s", exc)

    def flush_outbox(self) -> int:
        """Push buffered rows to Supabase and mark the outbox as drained."""
        if not self.cfg.supabase_ready or not os.path.exists(OUTBOX_PATH):
            return 0
        entries = self.read_outbox()
        if not entries:
            return 0
        flushed = 0
        remaining: List[Dict[str, Any]] = []
        for entry in entries:
            rows = entry.get("rows")
            table = entry.get("table", "")
            ok = self._insert(table, rows, on_conflict=entry.get("on_conflict"))
            if ok:
                flushed += 1
            else:
                remaining.append(entry)
        if not remaining:
            self._write_outbox([{"_note": f"outbox drained at {now_iso()}"}])
        else:
            self._write_outbox(remaining)
        self.log.info("Outbox flush: %d entries persisted, %d still pending", flushed, len(remaining))
        return flushed

    def read_outbox(self) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        # Prefer the working-copy file; fall back to the committed repo copy.
        raw = ""
        if os.path.exists(OUTBOX_PATH):
            try:
                raw = Path(OUTBOX_PATH).read_text(encoding="utf-8")
            except Exception as exc:
                self.log.warning("Could not read local outbox: %s", exc)
        if not raw and self.github is not None:
            raw = self.github.get_file_text(OUTBOX_PATH, ref=self.cfg.default_branch) or ""
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            if isinstance(parsed, dict) and parsed.get("table") and parsed.get("rows"):
                entries.append(parsed)
        return entries

    def _write_outbox(self, entries: List[Dict[str, Any]]) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            blob = "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in entries)
            Path(OUTBOX_PATH).write_text(blob, encoding="utf-8")
            if self.github is not None:
                self.github.commit_state_file(
                    OUTBOX_PATH,
                    blob or json.dumps({"_note": f"outbox empty at {now_iso()}"}) + "\n",
                    message="chore(abu-ali): sync agent outbox [skip ci]",
                )
        except Exception as exc:
            self.log.warning("Could not rewrite outbox: %s", exc)

    # -- high level API ----------------------------------------------------
    def start_task(self, *, repo: str, trigger: str, actor: str, issue_number: Optional[int],
                   issue_title: str, details: Optional[Dict[str, Any]] = None) -> Optional[int]:
        row = {
            "repo": repo,
            "trigger": trigger,
            "actor": actor,
            "issue_number": issue_number,
            "issue_title": truncate(issue_title, 300),
            "status": "running",
            "stage": "started",
            "summary": f"Abu Ali v{AGENT_VERSION} started ({trigger})",
            "details": details or {},
            "run_id": self.cfg.run_id,
            "run_url": self.cfg.run_url,
        }
        self._insert("tasks_log", row)
        rows = self._select("tasks_log", {
            "select": "id",
            "repo": f"eq.{repo}",
            "order": "id.desc",
            "limit": "1",
        })
        if rows:
            try:
                return int(rows[0].get("id"))
            except Exception:
                return None
        return None

    def finish_task(self, task_id: Optional[int], *, status: str, **fields: Any) -> None:
        if task_id is None:
            return
        payload = {"status": status, "updated_at": now_iso()}
        payload.update({k: v for k, v in fields.items() if v is not None})
        self._update("tasks_log", {"id": task_id}, payload)

    def remember(self, entries: Iterable[Dict[str, Any]], *, repo: str, source: str) -> int:
        rows: List[Dict[str, Any]] = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            content = truncate(str(entry.get("content") or "").strip(), 8000)
            if not content:
                continue
            key = slugify(str(entry.get("key") or content[:60]), max_len=80) or sha1_hex(content)[:12]
            tags = entry.get("tags")
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",") if t.strip()]
            if not isinstance(tags, list):
                tags = []
            try:
                importance = int(entry.get("importance", 5))
            except Exception:
                importance = 5
            rows.append({
                "repo": repo,
                "kind": truncate(str(entry.get("kind") or "fact"), 40),
                "key": key,
                "content": content,
                "source": truncate(source, 300),
                "tags": tags[:12],
                "importance": max(1, min(10, importance)),
                "metadata": {"agent_version": AGENT_VERSION, "run_id": self.cfg.run_id},
                "updated_at": now_iso(),
            })
        if not rows:
            return 0
        ok = self._insert("system_memory", rows, on_conflict="repo,key")
        self.log.info("Memory: stored %d entr%s (%s)", len(rows), "y" if len(rows) == 1 else "ies", "ok" if ok else "buffered")
        return len(rows) if ok else 0

    def recall(self, query: str, *, repo: str, limit: int = 12) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        tokens = keyword_tokens(query, limit=6)
        for token in tokens:
            rows = self._select("system_memory", {
                "select": "kind,key,content,tags,importance,updated_at",
                "repo": f"eq.{repo}",
                "content": f"ilike.*{token}*",
                "order": "importance.desc,updated_at.desc",
                "limit": str(max(1, limit // 2)),
            })
            results.extend(rows)
        if not results:
            results = self._select("system_memory", {
                "select": "kind,key,content,tags,importance,updated_at",
                "repo": f"eq.{repo}",
                "order": "importance.desc,updated_at.desc",
                "limit": str(limit),
            })
        seen = set()
        deduped = []
        for row in results:
            key = (row.get("key"), truncate(row.get("content"), 80))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(row)
        return deduped[:limit]

    def recent_tasks(self, *, repo: str, limit: int = 10) -> List[Dict[str, Any]]:
        return self._select("tasks_log", {
            "select": "id,created_at,issue_number,status,stage,summary,pr_url,llm_provider,llm_model,error",
            "repo": f"eq.{repo}",
            "order": "id.desc",
            "limit": str(limit),
        })

    def find_task(self, *, repo: str, issue_number: int, limit: int = 1) -> List[Dict[str, Any]]:
        return self._select("tasks_log", {
            "select": "id,created_at,updated_at,status,stage,pr_url,branch",
            "repo": f"eq.{repo}",
            "issue_number": f"eq.{issue_number}",
            "order": "id.desc",
            "limit": str(limit),
        })

    def record_interaction(self, *, repo: str, issue_number: Optional[int], author: str,
                           role: str, body: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        if not (body or "").strip():
            return
        self._insert("interactions", {
            "repo": repo,
            "issue_number": issue_number,
            "author": truncate(author, 120),
            "role": role,
            "body": truncate(body, 20000),
            "run_id": self.cfg.run_id,
            "metadata": metadata or {},
        })

    def history(self, *, repo: str, issue_number: int, limit: int = 12) -> List[Dict[str, Any]]:
        return self._select("interactions", {
            "select": "created_at,author,role,body",
            "repo": f"eq.{repo}",
            "issue_number": f"eq.{issue_number}",
            "order": "id.desc",
            "limit": str(limit),
        })

    def log_deployment(self, **fields: Any) -> None:
        row = {"repo": self.cfg.repo_full_name, "provider": "cloudflare"}
        row.update(fields)
        self._insert("deployments", row)


def _ddl_for(tables: Sequence[str]) -> str:
    """Extract only the CREATE statements for the requested tables from SCHEMA_SQL."""
    parts: List[str] = []
    for table in tables:
        match = re.search(
            rf"create table if not exists public\.{re.escape(table)}\b.*?\);",
            SCHEMA_SQL,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            parts.append(match.group(0))
    return "\n\n".join(parts)


def _supabase_project_ref(url: str) -> str:
    match = re.match(r"https?://([a-z0-9]+)\.supabase\.co", url or "", re.IGNORECASE)
    return match.group(1) if match else ""


# ===========================================================================
#  7. GITHUB CLIENT — issues in, branches + commits + PRs out
# ===========================================================================

class GitHubClient:
    """
    Thin, resilient wrapper over the GitHub REST API.

    Uses PyGithub when available for issue/PR ergonomics and always keeps a
    raw-REST path for everything (so a PyGithub version bump can never brick
    the agent).
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = LOG
        self.token = cfg.github_token
        self.api = cfg.api_base
        self.repo_full_name = cfg.repo_full_name
        self._gh = None
        self._py_repo = None
        self._default_branch_cache: Optional[str] = None
        if self.token and _PyGithub is not None:
            try:
                self._gh = _PyGithub(self.token, timeout=30, per_page=100)
                self._py_repo = self._gh.get_repo(self.repo_full_name) if self.repo_full_name else None
                self.log.info("PyGithub ready for %s", self.repo_full_name or "<no repo>")
            except Exception as exc:
                self.log.warning("PyGithub init failed (%s) — using REST only", exc)
                self._gh = None
                self._py_repo = None

    # -- plumbing ----------------------------------------------------------
    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.api}/repos/{self.repo_full_name}{path}" if path.startswith("/") else f"{self.api}/{path}"

    def available(self) -> bool:
        return bool(self.token and self.repo_full_name)

    def default_branch(self) -> str:
        if self._default_branch_cache:
            return self._default_branch_cache
        branch = ""
        try:
            body, status, _ = http_json("GET", self._url(""), label="gh repo info", headers=self.headers)
            if status == 200 and isinstance(body, dict) and body.get("default_branch"):
                branch = str(body["default_branch"])
        except Exception as exc:
            self.log.warning("Could not read the default branch (%s)", exc)
        self._default_branch_cache = branch or self.cfg.default_branch or "main"
        return self._default_branch_cache

    def get_issue(self, number: int) -> Optional[Dict[str, Any]]:
        try:
            body, status, _ = http_json("GET", self._url(f"/issues/{number}"), label=f"gh issue {number}", headers=self.headers, allow_404=True)
            if status == 200 and isinstance(body, dict):
                return body
        except Exception as exc:
            self.log.warning("get_issue(%s) failed: %s", number, exc)
        # PyGithub fallback
        if self._py_repo is not None:
            try:
                issue = self._py_repo.get_issue(number)
                return {
                    "number": issue.number,
                    "title": issue.title,
                    "body": issue.body or "",
                    "state": issue.state,
                    "labels": [{"name": label.name} for label in issue.labels],
                    "user": {"login": getattr(issue.user, "login", "")},
                    "created_at": issue.created_at.isoformat() if issue.created_at else None,
                    "updated_at": issue.updated_at.isoformat() if issue.updated_at else None,
                    "comments_url": issue.comments_url,
                    "html_url": issue.html_url,
                }
            except Exception as exc:
                self.log.warning("PyGithub get_issue(%s) failed: %s", number, exc)
        return None

    def list_issues(self, *, labels: Optional[Sequence[str]] = None, state: str = "open", limit: int = 20) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"state": state, "per_page": str(min(100, max(1, limit))), "sort": "updated", "direction": "desc"}
        if labels:
            params["labels"] = ",".join(labels)
        try:
            body, status, _ = http_json("GET", self._url("/issues"), label="gh list issues", headers=self.headers, params=params)
            if status == 200 and isinstance(body, list):
                return [issue for issue in body if "pull_request" not in issue]
        except Exception as exc:
            self.log.warning("list_issues failed: %s", exc)
        return []

    def list_issue_comments(self, number: int, limit: int = 100) -> List[Dict[str, Any]]:
        try:
            body, status, _ = http_json(
                "GET", self._url(f"/issues/{number}/comments"),
                label=f"gh comments {number}", headers=self.headers,
                params={"per_page": str(min(100, limit))},
            )
            if status == 200 and isinstance(body, list):
                return body
        except Exception as exc:
            self.log.warning("list_issue_comments(%s) failed: %s", number, exc)
        return []

    def is_agent_comment(self, comment: Dict[str, Any]) -> bool:
        """True when *we* wrote the comment (so we never answer ourselves)."""
        body = (comment.get("body") or "").lower()
        login = ((comment.get("user") or {}).get("login") or "").lower()
        if (comment.get("user") or {}).get("type") == "Bot":
            return True
        if login.endswith("[bot]") or login in (AGENT_HANDLE, "github-actions", "abu-ali-agent"):
            return True
        if "abu-ali-agent" in body or "<!-- abu-ali:" in body:
            return True
        return False

    def permission_of(self, username: str) -> str:
        """Return the repo permission level of a user ('admin','write','read','none')."""
        if not username:
            return "none"
        try:
            body, status, _ = http_json(
                "GET", self._url(f"/collaborators/{username}/permission"),
                label=f"gh permission {username}", headers=self.headers, allow_404=True,
            )
            if status == 200 and isinstance(body, dict):
                return str(body.get("permission") or "none").lower()
        except Exception as exc:
            self.log.warning("permission_of(%s) failed: %s", username, exc)
        return "none"

    PERMISSION_RANK = {"none": 0, "read": 1, "triage": 2, "write": 3, "maintain": 4, "admin": 5}

    def has_min_permission(self, username: str, minimum: str) -> bool:
        return self.PERMISSION_RANK.get(self.permission_of(username), 0) >= self.PERMISSION_RANK.get(minimum, 3)

    # -- comments ----------------------------------------------------------
    def post_comment(self, issue_number: int, body: str) -> bool:
        if self.cfg.dry_run:
            self.log.info("[dry-run] would comment on #%s (%d chars)", issue_number, len(body))
            return True
        if AGENT_MARKER not in body:
            body = body.rstrip() + "\n\n" + AGENT_MARKER
        chunks = chunk_text(body, MAX_COMMENT_CHARS)
        for index, chunk in enumerate(chunks):
            suffix = "" if len(chunks) == 1 else f"\n\n_(part {index + 1}/{len(chunks)})_"
            try:
                _, status, _ = http_json(
                    "POST", self._url(f"/issues/{issue_number}/comments"),
                    label=f"gh comment {issue_number}", headers=self.headers,
                    json_body={"body": chunk + suffix}, ok_status=(200, 201),
                )
                if status not in (200, 201):
                    self.log.error("Comment on #%s returned HTTP %s", issue_number, status)
                    return False
            except Exception as exc:
                self.log.error("post_comment(%s) failed: %s", issue_number, exc)
                return False
        self.log.info("Posted a comment on issue #%s", issue_number)
        return True

    def set_labels(self, issue_number: int, *, add: Sequence[str] = (), remove: Sequence[str] = ()) -> bool:
        if self.cfg.dry_run:
            return True
        ok = True
        if add:
            try:
                http_json("POST", self._url(f"/issues/{issue_number}/labels"), label="gh add labels",
                          headers=self.headers, json_body={"labels": list(add)}, ok_status=(200, 201))
            except Exception as exc:
                self.log.warning("add labels failed: %s", exc)
                ok = False
        for label in remove:
            try:
                http_json("DELETE", self._url(f"/issues/{issue_number}/labels/{requests.utils.quote(label)}"),
                          label="gh remove label", headers=self.headers, ok_status=(200, 204), allow_404=True)
            except Exception as exc:
                self.log.debug("remove label %s failed: %s", label, exc)
        return ok

    def ensure_labels_exist(self) -> None:
        """Create the agent's labels once, if they are missing (additive only)."""
        if self.cfg.dry_run:
            return
        palette = {
            "abu-ali": "5319e7",
            "task": "1d76db",
            "devops": "0e8a16",
            "agent-report": "fbca04",
            "agent-failure": "b60205",
            "needs-human": "d93f0b",
            "auto-fix": "c2e0c6",
        }
        try:
            _, status, _ = http_json("GET", self._url("/labels"), label="gh labels", headers=self.headers, params={"per_page": "100"})
        except Exception as exc:
            self.log.debug("label listing failed: %s", exc)
            return
        for name, color in palette.items():
            try:
                _, code, _ = http_json(
                    "POST", self._url("/labels"), label=f"gh create label {name}", headers=self.headers,
                    json_body={"name": name, "color": color, "description": f"{AGENT_NAME} agent label"},
                    ok_status=(200, 201), allow_404=True,
                )
                if code in (200, 201):
                    self.log.debug("created label %s", name)
            except Exception:
                pass  # 422 = already exists, which is exactly what we want

    # -- git data ----------------------------------------------------------
    def get_ref_sha(self, branch: str) -> Optional[str]:
        try:
            body, status, _ = http_json("GET", self._url(f"/git/ref/heads/{branch}"), label=f"gh ref {branch}",
                                        headers=self.headers, allow_404=True)
            if status == 200 and isinstance(body, dict):
                return ((body.get("object") or {}).get("sha")) or None
        except Exception as exc:
            self.log.warning("get_ref_sha(%s) failed: %s", branch, exc)
        return None

    def ensure_branch(self, branch: str, base: Optional[str] = None) -> Optional[str]:
        """Create `branch` from `base` when missing. Returns the head SHA."""
        base = base or self.default_branch()
        existing = self.get_ref_sha(branch)
        if existing:
            return existing
        base_sha = self.get_ref_sha(base)
        if not base_sha:
            self.log.error("Cannot resolve base branch %s", base)
            return None
        if self.cfg.dry_run:
            self.log.info("[dry-run] would create branch %s from %s", branch, base)
            return base_sha
        try:
            _, status, _ = http_json(
                "POST", self._url("/git/refs"), label=f"gh create branch {branch}", headers=self.headers,
                json_body={"ref": f"refs/heads/{branch}", "sha": base_sha}, ok_status=(200, 201),
            )
            if status in (200, 201):
                self.log.info("Created branch %s from %s (%s)", branch, base, base_sha[:8])
                return base_sha
        except Exception as exc:
            self.log.warning("create branch failed (%s) — will reuse it if it now exists", exc)
        return self.get_ref_sha(branch)

    def get_file(self, path: str, ref: Optional[str] = None) -> Optional[Dict[str, Any]]:
        params = {"ref": ref} if ref else None
        try:
            body, status, _ = http_json("GET", self._url(f"/contents/{path}"), label=f"gh get file {path}",
                                        headers=self.headers, params=params, allow_404=True)
            if status == 200 and isinstance(body, dict) and body.get("type") == "file":
                return body
        except Exception as exc:
            self.log.debug("get_file(%s) failed: %s", path, exc)
        return None

    def get_file_text(self, path: str, ref: Optional[str] = None) -> Optional[str]:
        meta = self.get_file(path, ref=ref)
        if not meta:
            return None
        if meta.get("encoding") == "base64" and meta.get("content"):
            return b64decode(meta["content"])
        if meta.get("download_url"):
            try:
                response = HTTP_SESSION.get(meta["download_url"], timeout=60)
                if response.status_code == 200:
                    return response.text
            except Exception as exc:
                self.log.debug("download_url fetch failed for %s: %s", path, exc)
        return None

    def list_tree(self, ref: Optional[str] = None) -> List[Dict[str, Any]]:
        ref = ref or self.default_branch()
        try:
            body, status, _ = http_json("GET", self._url(f"/git/trees/{ref}"), label="gh tree",
                                        headers=self.headers, params={"recursive": "1"})
            if status == 200 and isinstance(body, dict):
                return [item for item in (body.get("tree") or []) if isinstance(item, dict)]
        except Exception as exc:
            self.log.warning("list_tree failed: %s", exc)
        return []

    def commit_files(
        self,
        branch: str,
        files: Sequence[Dict[str, str]],
        message: str,
        *,
        deletions: Sequence[str] = (),
        allow_protected: bool = False,
    ) -> Optional[str]:
        """
        Commit a set of file writes (and optional deletions) to `branch` in one
        atomic commit. Returns the new commit SHA.

        `files` items: {"path": "...", "content": "..."} where content may also
        be supplied pre-encoded as {"content_b64": "..."}.
        """
        files = [f for f in files if f.get("path")]
        deletions = [d for d in deletions if d]
        if not files and not deletions:
            self.log.info("Nothing to commit")
            return None
        if self.get_ref_sha(branch) is None:
            self.log.error("Branch %s does not exist — create it first", branch)
            return None

        accepted: List[Dict[str, Any]] = []
        rejected: List[str] = []
        total_bytes = 0
        for item in files[:MAX_FILES_PER_PR]:
            path = str(item["path"]).lstrip("/")
            if _is_protected_path(path) and not (allow_protected or ALLOW_PROTECTED_WRITES):
                rejected.append(path)
                continue
            raw = item.get("content_b64")
            content = item.get("content")
            encoded = raw if raw else b64encode("" if content is None else str(content))
            size = len(encoded) * 3 // 4
            if size > MAX_FILE_BYTES:
                rejected.append(f"{path} (too large: {size} bytes)")
                continue
            if total_bytes + size > MAX_TOTAL_BYTES:
                rejected.append(f"{path} (run budget exceeded)")
                continue
            total_bytes += size
            accepted.append({"path": path, "content": encoded})

        if len(files) > MAX_FILES_PER_PR:
            rejected.extend([f"{f['path']} (over per-PR file budget)" for f in files[MAX_FILES_PER_PR:]])
        if rejected:
            self.log.warning("Skipped by safety policy: %s", ", ".join(rejected[:12]))

        if not accepted and not deletions:
            self.log.error("Every file was rejected by the safety policy — nothing to commit")
            return None
        if self.cfg.dry_run:
            self.log.info("[dry-run] would commit %d file(s) to %s: %s", len(accepted), branch, message.splitlines()[0])
            for item in accepted:
                self.log.info("[dry-run]   + %s (%d bytes)", item["path"], len(item["content"]) * 3 // 4)
            for path in deletions:
                self.log.info("[dry-run]   - %s", path)
            return "dry-run-sha"

        def _commit() -> Optional[str]:
            # 1) blobs
            tree_items: List[Dict[str, Any]] = []
            for item in accepted:
                blob, status, _ = http_json("POST", self._url("/git/blobs"), label=f"gh blob {item['path']}",
                                            headers=self.headers,
                                            json_body={"content": item["content"], "encoding": "base64"},
                                            ok_status=(200, 201))
                if status not in (200, 201) or not isinstance(blob, dict) or not blob.get("sha"):
                    raise RetryableError(f"blob upload failed for {item['path']} (HTTP {status})")
                tree_items.append({"path": item["path"], "mode": "100644", "type": "blob", "sha": blob["sha"]})

            for path in deletions:
                path = str(path).lstrip("/")
                if _is_protected_path(path) and not (allow_protected or ALLOW_PROTECTED_WRITES):
                    continue
                tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": None})

            # 2) tree on top of the branch head
            head_sha = self._require_sha(branch)
            parent_commit, status, _ = http_json("GET", self._url(f"/git/commits/{head_sha}"), label="gh commit info",
                                                 headers=self.headers)
            base_tree = (parent_commit or {}).get("tree", {}).get("sha") if isinstance(parent_commit, dict) else None
            if not base_tree:
                raise PermanentError(f"cannot resolve the tree of {head_sha}")

            tree_body: Dict[str, Any] = {"base_tree": base_tree, "tree": tree_items}
            tree, status, _ = http_json("POST", self._url("/git/trees"), label="gh create tree",
                                        headers=self.headers, json_body=tree_body, ok_status=(200, 201))
            if status not in (200, 201) or not isinstance(tree, dict) or not tree.get("sha"):
                raise PermanentError(f"tree creation failed (HTTP {status})")

            # 3) commit
            author = _commit_author(self.cfg)
            commit_body = {"message": truncate(message, 4000), "tree": tree["sha"], "parents": [head_sha], **author}
            commit, status, _ = http_json("POST", self._url("/git/commits"), label="gh create commit",
                                          headers=self.headers, json_body=commit_body, ok_status=(200, 201))
            if status not in (200, 201) or not isinstance(commit, dict) or not commit.get("sha"):
                raise PermanentError(f"commit creation failed (HTTP {status})")

            # 4) move the branch (force=false → safe, fast-forward only)
            _, status, _ = http_json("PATCH", self._url(f"/git/refs/heads/{branch}"), label="gh update ref",
                                     headers=self.headers, json_body={"sha": commit["sha"], "force": False},
                                     ok_status=(200, 201))
            if status not in (200, 201):
                raise PermanentError(f"ref update failed (HTTP {status})")
            return str(commit["sha"])

        sha = call_with_retry(f"commit to {branch}", _commit, RetryPolicy(attempts=4, base_delay=2.0, max_delay=45.0),
                              retry_on=(RetryableError,), logger=self.log)
        self.log.info("Committed %s to %s (%d file(s), %d deletion(s))", (sha or "")[:8], branch, len(accepted), len(deletions))
        return sha

    def _require_sha(self, branch: str) -> str:
        sha = self.get_ref_sha(branch)
        if not sha:
            raise PermanentError(f"branch {branch} has no head")
        return sha

    def commit_state_file(self, path: str, content: str, message: str) -> bool:
        """Commit agent bookkeeping files (outbox/state) straight to the default branch."""
        branch = self.default_branch()
        if self.cfg.dry_run:
            self.log.info("[dry-run] would store %s on %s", path, branch)
            return True
        try:
            existing = self.get_file(path, ref=branch)
        except Exception:
            existing = None
        payload: Dict[str, Any] = {"message": message, "content": b64encode(content), "branch": branch}
        if existing and existing.get("sha"):
            payload["sha"] = existing["sha"]
        try:
            _, status, _ = http_json("PUT", self._url(f"/contents/{path}"), label=f"gh state file {path}",
                                     headers=self.headers, json_body=payload, ok_status=(200, 201))
            return status in (200, 201)
        except Exception as exc:
            self.log.warning("commit_state_file(%s) failed: %s", path, exc)
            return False

    # -- pull requests -----------------------------------------------------
    def open_pr(self, *, head: str, title: str, body: str, base: Optional[str] = None,
                draft: bool = False) -> Optional[Dict[str, Any]]:
        base = base or self.default_branch()
        if self.cfg.dry_run:
            self.log.info("[dry-run] would open PR %s → %s", head, base)
            return {"number": 0, "html_url": "", "dry_run": True}
        existing = self.find_open_pr(head)
        if existing:
            self.log.info("Reusing open PR #%s for %s", existing.get("number"), head)
            return existing
        try:
            pr, status, _ = http_json(
                "POST", self._url("/pulls"), label="gh create PR", headers=self.headers,
                json_body={"title": truncate(title, 250), "head": head, "base": base,
                           "body": truncate(body, MAX_COMMENT_CHARS), "draft": draft,
                           "maintainer_can_modify": True},
                ok_status=(200, 201),
            )
            if status in (200, 201) and isinstance(pr, dict):
                self.log.info("Opened PR #%s → %s", pr.get("number"), pr.get("html_url"))
                return pr
        except Exception as exc:
            self.log.error("open_pr failed: %s", exc)
        return None

    def find_open_pr(self, head_branch: str) -> Optional[Dict[str, Any]]:
        try:
            owner = self.cfg.repo_owner
            body, status, _ = http_json("GET", self._url("/pulls"), label="gh list PRs", headers=self.headers,
                                        params={"head": f"{owner}:{head_branch}", "state": "open", "per_page": "5"})
            if status == 200 and isinstance(body, list) and body:
                return body[0]
        except Exception as exc:
            self.log.debug("find_open_pr failed: %s", exc)
        return None

    def merge_pr(self, number: int, method: Optional[str] = None) -> bool:
        if self.cfg.dry_run:
            self.log.info("[dry-run] would merge PR #%s", number)
            return True
        method = method or self.cfg.merge_method or "squash"
        try:
            _, status, _ = http_json(
                "PUT", self._url(f"/pulls/{number}/merge"), label=f"gh merge PR {number}", headers=self.headers,
                json_body={"merge_method": method}, ok_status=(200,), allow_404=True,
            )
            if status == 200:
                self.log.info("Merged PR #%s using %s", number, method)
                return True
            self.log.warning("PR #%s not merged (HTTP %s)", number, status)
        except Exception as exc:
            self.log.warning("merge_pr(%s) failed: %s", number, exc)
        return False

    def find_open_prs_for_branch(self, branch: str) -> bool:
        return self.find_open_pr(branch) is not None


def _is_protected_path(path: str) -> bool:
    normalized = path.lstrip("/").lower()
    for blocked in PROTECTED_PATHS:
        if normalized == blocked or normalized.startswith(blocked):
            return True
    if normalized.startswith(".env") or "/.env" in normalized:
        return True
    return False


def _commit_author(cfg: Config) -> Dict[str, Any]:
    return {
        "author": {
            "name": f"{AGENT_NAME} (Autonomous DevOps Architect)",
            "email": os.getenv("AGENT_EMAIL", "abu-ali-agent@users.noreply.github.com"),
        },
        "committer": {
            "name": f"{AGENT_NAME} Bot",
            "email": os.getenv("AGENT_EMAIL", "abu-ali-agent@users.noreply.github.com"),
        },
    }


# ===========================================================================
#  8. MCP — MODEL CONTEXT PROTOCOL CLIENT (GitHub / Supabase / Context7)
# ===========================================================================

class MCPClient:
    """
    Minimal, spec-compliant MCP client over Streamable HTTP (JSON-RPC 2.0).

    Handshake: `initialize` → `notifications/initialized` → `tools/list` → `tools/call`.
    Every call is wrapped in try/except and returns a structured result instead
    of raising, so a broken MCP server can never stop a run.
    """

    PROTOCOL_VERSION = "2025-06-18"

    def __init__(self, cfg: Config, servers: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        self.cfg = cfg
        self.log = LOG
        self.servers = servers if servers is not None else dict(cfg.mcp_servers)
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.tools: Dict[str, List[Dict[str, Any]]] = {}
        self.log.info("MCP servers configured: %s", ", ".join(sorted(self.servers.keys())) or "none")

    # -- transport ---------------------------------------------------------
    def _headers(self, spec: Dict[str, Any], session_id: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.PROTOCOL_VERSION,
        }
        key_env = spec.get("apiKeyEnv")
        token = (os.getenv(key_env, "") if key_env else "") or (spec.get("apiKey") or "")
        if token:
            register_secret(token)
            headers["Authorization"] = f"Bearer {token}"
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    def _rpc(self, server: str, method: str, params: Optional[Dict[str, Any]] = None,
             *, timeout: int = 60, notify: bool = False) -> Optional[Any]:
        spec = self.servers.get(server)
        if not spec:
            return None
        url = spec.get("url")
        if not url:
            return None
        session = self.sessions.get(server, {})
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notify:
            payload["id"] = session.get("next_id", 1)
            session["next_id"] = payload["id"] + 1
            self.sessions[server] = session

        def _attempt() -> Optional[Any]:
            try:
                response = HTTP_SESSION.post(
                    url,
                    headers=self._headers(spec, session.get("id")),
                    json=payload,
                    timeout=timeout,
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                raise RetryableError(f"MCP {server}/{method}: {exc}") from exc

            if response.status_code == 401 or response.status_code == 403:
                raise PermanentError(f"MCP {server}: auth rejected (HTTP {response.status_code})")
            if response.status_code == 404:
                raise PermanentError(f"MCP {server}: endpoint not found ({url})")
            if response.status_code >= 500 or response.status_code == 429:
                raise RetryableError(f"MCP {server}: HTTP {response.status_code}",
                                     retry_after=retry_after_from_headers(dict(response.headers)))
            new_session = response.headers.get("Mcp-Session-Id") or response.headers.get("mcp-session-id")
            if new_session:
                session["id"] = new_session
                self.sessions[server] = session
            if notify:
                return True
            body = _parse_mcp_body(response)
            if body is None:
                return None
            if isinstance(body, dict) and body.get("error"):
                raise PermanentError(f"MCP {server}/{method}: {json_dumps_safe(body['error'], 400)}")
            return (body or {}).get("result") if isinstance(body, dict) else None

        try:
            return call_with_retry(f"mcp {server}/{method}", _attempt,
                                   RetryPolicy(attempts=2, base_delay=2.0, max_delay=20.0),
                                   retry_on=(RetryableError,), logger=self.log)
        except Exception as exc:
            self.log.warning("MCP call %s/%s failed: %s", server, method, exc)
            return None

    # -- public API --------------------------------------------------------
    def init_session(self, server: str) -> bool:
        spec = self.servers.get(server)
        if not spec:
            return False
        if self.sessions.get(server, {}).get("ready"):
            return True
        result = self._rpc(server, "initialize", {
            "protocolVersion": self.PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}, "sampling": {}},
            "clientInfo": {"name": "abu-ali-agent", "version": AGENT_VERSION},
        })
        if not isinstance(result, dict):
            self.log.warning("MCP %s did not complete the handshake", server)
            return False
        self._rpc(server, "notifications/initialized", {}, notify=True)
        session = self.sessions.setdefault(server, {})
        session["ready"] = True
        session["serverInfo"] = result.get("serverInfo", {})
        self.sessions[server] = session
        self.log.info("MCP %s ready: %s", server, json_dumps_safe(result.get("serverInfo", {}), 200))
        return True

    def list_tools(self, server: str) -> List[Dict[str, Any]]:
        if server in self.tools:
            return self.tools[server]
        if not self.init_session(server):
            return []
        result = self._rpc(server, "tools/list", {})
        tools = (result or {}).get("tools") if isinstance(result, dict) else None
        tools = tools if isinstance(tools, list) else []
        self.tools[server] = tools
        self.log.info("MCP %s exposes %d tool(s)", server, len(tools))
        return tools

    def call_tool(self, server: str, tool: str, arguments: Dict[str, Any]) -> Optional[Any]:
        if not self.init_session(server):
            return None
        result = self._rpc(server, "tools/call", {"name": tool, "arguments": arguments or {}}, timeout=90)
        if isinstance(result, dict) and result.get("isError"):
            self.log.warning("MCP tool %s/%s reported an error: %s", server, tool, json_dumps_safe(result, 400))
        return result

    @staticmethod
    def result_text(result: Any) -> str:
        """Flatten an MCP tool result into plain text."""
        if not result:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        parts.append(item.get("text") or json_dumps_safe(item.get("json", item), 4000))
                    else:
                        parts.append(str(item))
                return "\n".join(part for part in parts if part)
            if result.get("structuredContent") is not None:
                return json_dumps_safe(result["structuredContent"], 6000)
            return json_dumps_safe(result, 6000)
        return str(result)

    def context7_docs(self, library: str, topic: str = "") -> str:
        """
        Fetch up-to-date library documentation through Context7 MCP.
        Returns the docs as text, or "" when the tool is unavailable.
        """
        if "context7" not in self.servers:
            return ""
        try:
            tools = {tool.get("name") for tool in self.list_tools("context7")}
            if "resolve-library-id" in tools:
                resolved = self.call_tool("context7", "resolve-library-id", {"libraryName": library})
                text = self.result_text(resolved)
                match = re.search(r"(/[\w\-\.]+/[\w\-\.]+)", text)
                if match:
                    library = match.group(1)
            if "get-library-docs" in tools:
                docs = self.call_tool("context7", "get-library-docs", {
                    "context7CompatibleLibraryID": library,
                    "topic": topic,
                    "tokens": 4000,
                })
                return truncate(self.result_text(docs), 8000)
        except Exception as exc:
            self.log.warning("Context7 lookup failed for %s: %s", library, exc)
        return ""

    def describe(self) -> List[Dict[str, Any]]:
        described = []
        for name, spec in self.servers.items():
            described.append({
                "name": name,
                "url": spec.get("url"),
                "transport": spec.get("transport", "http"),
                "enabled": True,
                "authenticated": bool(spec.get("apiKeyEnv") and os.getenv(str(spec.get("apiKeyEnv")), "")),
            })
        return described


def _parse_mcp_body(response: requests.Response) -> Optional[Dict[str, Any]]:
    """Parse either a plain JSON body or an SSE (text/event-stream) MCP response."""
    content_type = (response.headers.get("content-type") or "").lower()
    text = response.text or ""
    if "text/event-stream" in content_type or text.lstrip().startswith("event:") or "\ndata:" in text:
        last: Optional[Dict[str, Any]] = None
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            parsed = safe_json_loads(payload)
            if isinstance(parsed, dict):
                last = parsed
        return last
    return safe_json_loads(text)


# ===========================================================================
#  9. CLOUDFLARE — WORKERS & PAGES DEPLOYMENTS
# ===========================================================================

class CloudflareClient:
    """Deploy Workers / create Pages deployments and poll them to completion."""

    API = "https://api.cloudflare.com/client/v4"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = LOG
        self.account_id = cfg.cloudflare_account_id
        self.token = cfg.cloudflare_api_token
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": f"AbuAli-Agent/{AGENT_VERSION}",
        }

    @property
    def available(self) -> bool:
        return bool(self.account_id and self.token)

    def _call(self, method: str, path: str, *, label: str, json_body: Any = None,
              ok_status: Sequence[int] = (200, 201), timeout: int = 120) -> Optional[Any]:
        if not self.available:
            return None
        def _attempt() -> Optional[Any]:
            body, status, _ = http_json(
                method, f"{self.API}{path}", label=label, headers=self.headers, json_body=json_body,
                ok_status=ok_status, timeout=timeout, policy=RetryPolicy(attempts=3, base_delay=2.0, max_delay=45.0),
                allow_404=True,
            )
            if status == 404:
                raise PermanentError(f"{label}: not found ({path})")
            if isinstance(body, dict) and body.get("success") is False:
                errors = json_dumps_safe(body.get("errors"), 400)
                if any(code in errors for code in ("10000", "1000", "6003")):  # auth / permission
                    raise PermanentError(f"{label}: {errors}")
                raise RetryableError(f"{label}: {errors}")
            return body.get("result") if isinstance(body, dict) else body

        try:
            return call_with_retry(label, _attempt, RetryPolicy(attempts=3, base_delay=2.0, max_delay=45.0),
                                   retry_on=(RetryableError,), logger=self.log)
        except Exception as exc:
            self.log.error("%s failed: %s", label, exc)
            return None

    # -- discovery ---------------------------------------------------------
    def verify_token(self) -> bool:
        if not self.available:
            self.log.info("Cloudflare is not configured — skipping edge verification")
            return False
        result = self._call("GET", "/user/tokens/verify", label="cf verify token")
        if isinstance(result, dict) and result.get("status") == "active":
            self.log.info("Cloudflare API token verified ✔")
            return True
        self.log.warning("Cloudflare token verification did not return an active status")
        return False

    def list_workers(self) -> List[str]:
        result = self._call("GET", f"/accounts/{self.account_id}/workers/scripts", label="cf list workers")
        if isinstance(result, list):
            return [str(item.get("id")) for item in result if isinstance(item, dict) and item.get("id")]
        return []

    def list_pages_projects(self) -> List[Dict[str, Any]]:
        result = self._call("GET", f"/accounts/{self.account_id}/pages/projects", label="cf list pages")
        return result if isinstance(result, list) else []

    # -- deploy ------------------------------------------------------------
    def deploy_worker(self, name: str, script: str, *, compatibility_date: Optional[str] = None,
                      compatibility_flags: Optional[Sequence[str]] = None,
                      bindings: Optional[Sequence[Dict[str, Any]]] = None,
                      module: bool = True) -> Dict[str, Any]:
        """
        Upload (create or update) a Worker script. Workers are never deleted —
        only create/update, in line with the "never destroy anything" rule.
        """
        if not self.available:
            return {"status": "skipped", "reason": "cloudflare not configured"}
        metadata = {
            "main_module" if module else "body_part": "worker.js" if module else "worker",
            "compatibility_date": compatibility_date or utcnow().strftime("%Y-%m-%d"),
        }
        if compatibility_flags:
            metadata["compatibility_flags"] = list(compatibility_flags)
        if bindings:
            metadata["bindings"] = list(bindings)
        payload = {"metadata": metadata}
        if module:
            payload["worker.js"] = [{"name": "worker.js", "type": "application/javascript+module", "content": script}]
        else:
            payload["worker"] = script

        self.log.info("Deploying Cloudflare Worker '%s' (%d bytes)", name, len(script))
        result = self._call("PUT", f"/accounts/{self.account_id}/workers/scripts/{name}",
                            label=f"cf deploy worker {name}", json_body=payload)
        if result is None:
            return {"status": "failed", "target": name, "reason": "API call failed"}
        return {"status": "deployed", "target": name, "target_type": "worker",
                "result": result if isinstance(result, dict) else {"raw": truncate(str(result), 500)}}

    def create_pages_deployment(self, project: str, *, branch: Optional[str] = None,
                                commit_sha: Optional[str] = None,
                                commit_message: Optional[str] = None) -> Dict[str, Any]:
        """Trigger a Pages build from the connected Git branch (no destructive action)."""
        if not self.available:
            return {"status": "skipped", "reason": "cloudflare not configured"}
        body: Dict[str, Any] = {}
        if branch:
            body["branch"] = branch
        if commit_sha:
            body["commit_sha"] = commit_sha
            body["commit_message"] = commit_message or f"Deploy by {AGENT_NAME}"
        self.log.info("Triggering Cloudflare Pages deployment for '%s'", project)
        result = self._call("POST", f"/accounts/{self.account_id}/pages/projects/{project}/deployments",
                            label=f"cf pages deploy {project}", json_body=body or None)
        if not isinstance(result, dict):
            return {"status": "failed", "target": project, "reason": "API call failed"}
        deployment_id = result.get("id")
        url = result.get("url") or ""
        if deployment_id:
            url = self._poll_pages_deployment(project, str(deployment_id)) or url
        return {"status": "deployed", "target": project, "target_type": "pages",
                "deployment_id": deployment_id, "url": url, "result": result}

    def _poll_pages_deployment(self, project: str, deployment_id: str, attempts: int = 12) -> Optional[str]:
        for attempt in range(1, attempts + 1):
            result = self._call("GET", f"/accounts/{self.account_id}/pages/projects/{project}/deployments/{deployment_id}",
                                label="cf pages status")
            if not isinstance(result, dict):
                return None
            stage = (result.get("latest_stage") or {}).get("name")
            status = (result.get("latest_stage") or {}).get("status")
            self.log.info("Pages deployment %s: stage=%s status=%s", str(deployment_id)[:8], stage, status)
            if status == "success" or result.get("url"):
                return result.get("url")
            if status == "failure" or stage == "deploy" and status == "failure":
                return None
            time.sleep(min(5 * attempt, 20))
        return None

    def deploy(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a deployment spec produced by the brain:
          {"type": "worker", "name": "my-worker", "script": "...",
           "compatibility_date": "2024-01-01", "compatibility_flags": ["nodejs_compat"]}
          {"type": "pages", "project": "my-site", "branch": "main", "commit_sha": "..."}
        """
        if not isinstance(spec, dict):
            return {"status": "skipped", "reason": "invalid deployment spec"}
        kind = str(spec.get("type") or spec.get("target_type") or "").lower()
        try:
            if kind == "worker":
                name = spec.get("name") or spec.get("target")
                script = spec.get("script") or spec.get("content") or ""
                if not name or not script.strip():
                    return {"status": "skipped", "reason": "worker spec needs 'name' and 'script'"}
                return self.deploy_worker(
                    str(name), str(script),
                    compatibility_date=spec.get("compatibility_date"),
                    compatibility_flags=spec.get("compatibility_flags"),
                    bindings=spec.get("bindings"),
                )
            if kind == "pages":
                project = spec.get("project") or spec.get("name") or self.cfg.cloudflare_pages_project
                if not project:
                    return {"status": "skipped", "reason": "pages spec needs 'project'"}
                return self.create_pages_deployment(
                    str(project), branch=spec.get("branch") or self.cfg.default_branch,
                    commit_sha=spec.get("commit_sha"), commit_message=spec.get("commit_message"),
                )
            return {"status": "skipped", "reason": f"unsupported deployment type '{kind}'"}
        except Exception as exc:
            self.log.error("Deployment failed: %s", exc)
            return {"status": "failed", "target": str(spec.get("name") or spec.get("project") or ""), "reason": str(exc)}


# ===========================================================================
# 10. TASK CONTEXT & PROMPTS
# ===========================================================================

@dataclass
class TaskContext:
    """Everything the brain needs to know about one unit of work."""

    trigger: str = "manual"
    instruction: str = ""
    issue_number: Optional[int] = None
    issue_title: str = ""
    issue_body: str = ""
    issue_author: str = ""
    issue_url: str = ""
    labels: List[str] = field(default_factory=list)
    comment_body: str = ""
    comment_author: str = ""
    comment_id: Optional[int] = None
    is_sweep: bool = False
    info_only: bool = False          # pure lookup commands never touch the repo
    files: List[Dict[str, str]] = field(default_factory=list)
    tool_outputs: Dict[str, Any] = field(default_factory=dict)
    memories: List[Dict[str, Any]] = field(default_factory=list)
    history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.issue_title or truncate(first_line(self.instruction or "general maintenance"), 90)

    def brief(self) -> str:
        lines = [f"trigger={self.trigger}"]
        if self.issue_number:
            lines.append(f"issue=#{self.issue_number}")
        lines.append(f"author={self.issue_author or self.comment_author or 'n/a'}")
        return " | ".join(lines)


CHANGE_VERBS = (
    # English
    "add", "create", "build", "implement", "fix", "update", "change", "refactor", "remove",
    "delete", "rename", "move", "deploy", "install", "upgrade", "downgrade", "write", "make",
    "improve", "optimize", "migrate", "enable", "disable", "configure", "setup", "set up",
    # Arabic (common imperatives/requests)
    "أضف", "اضف", "أنشئ", "انشئ", "سوّي", "سوي", "عدّل", "عدل", "صلّح", "صلح", "أصلح", "اصلح",
    "غيّر", "غير", "احذف", "امسح", "ارفع", "انشر", "ثبّت", "حدّث", "حدث", "طوّر", "طور",
    "اعمل", "ابني", "سجّل", "سجل", "هيّئ", "هيئ", "انقل", "راجع", "ضيف",
)


def _looks_like_change_request(text: str) -> bool:
    """True when the wording asks for a modification rather than a lookup."""
    lowered = (text or "").lower()
    return any(verb in lowered for verb in CHANGE_VERBS)


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


class PromptBuilder:
    """
    Builds the system prompt and the per-stage user prompts.

    Everything the agent knows about the world (repo map, memory, history,
    tool results, personality rules) is injected here — the code stays generic,
    the prompts stay specific.
    """

    def __init__(self, cfg: Config, agent: "Agent") -> None:
        self.cfg = cfg
        self.agent = agent

    # -- static personality -------------------------------------------------
    def system(self) -> str:
        language = "Arabic (Iraqi-friendly, clear Modern Standard Arabic)" if self.cfg.language.startswith("ar") else "English"
        return f"""You are {AGENT_NAME} — an Autonomous AI DevOps Architect and Senior Software Engineer.
You run inside GitHub Actions, you are commanded from a phone through GitHub Issues, and you ship real code.

## Identity
- Repo: {self.cfg.repo_full_name or "unknown"} (default branch: {self.cfg.default_branch})
- Brain: Groq for speed, Gemini for depth (you do not decide this, the router does)
- Memory: Supabase (tasks_log, system_memory, interactions, deployments)
- Edge: Cloudflare Workers & Pages
- Version: {AGENT_VERSION}

## Non-negotiable rules
1. NEVER delete, rename or destroy existing features, tables, files or data. Additive work only.
   Refactors that rewrite existing behaviour require an explicit request in the issue.
2. NEVER write secrets, tokens or credentials into code. Read them from environment variables.
3. NEVER invent APIs. If you need an up-to-date library API, call the context7_docs tool.
4. ALWAYS keep changes inside the files you declare, and keep them small, runnable and complete.
5. ALWAYS log your reasoning in the final summary: what you changed, why, and the risk.
6. If the task is ambiguous or dangerous, stop and call the `ask_human` tool instead of guessing.
7. Preserve backward compatibility; prefer new files over rewriting working ones.
8. Every deliverable must be production quality: imports, error handling, retries, logging.

## Language
Write every human-facing text (issue comments, PR descriptions, summaries) in {language}.
Keep code, identifiers, commit messages and file paths in English.
{self._constitution_block()}"""

    # -- stage 1: reconnaissance -------------------------------------------
    def plan_prompt(self, ctx: TaskContext) -> str:
        repo_map = self.agent.build_repo_map()
        memory = self._memory_block(ctx)
        files_block = self._files_block(ctx)
        tools_block = self._tools_block(ctx)
        history = self._history_block(ctx)

        quote = '"' * 3
        if ctx.comment_body:
            comment_block = (
                f"New comment from @{ctx.comment_author}:\n"
                f"{quote}\n{truncate(ctx.comment_body, 4000)}\n{quote}"
            )
        else:
            comment_block = ""

        return f"""# TASK INTAKE

## Trigger
{ctx.brief()}

## Request (verbatim)
Title: {ctx.issue_title or "(none)"}
Body:
{quote}
{truncate(ctx.issue_body, 6000) or "(no body)"}
{quote}

{comment_block}

## Repository map (default branch)
{repo_map}

## Relevant long-term memory (Supabase)
{memory}

{history}

## Files the user attached / referenced
{files_block}

## Raw output of the tools you already ran
{tools_block}

# YOUR JOB — STAGE 1 ONLY: PLAN

Think step by step but answer with ONE JSON object, no markdown fences:

{{
  "intent": "one sentence: what the user actually wants",
  "task_type": "code|config|deploy|docs|answer|maintenance",
  "risk": "low|medium|high",
  "needs_human": false,
  "needs_human_reason": "",
  "summary": "2-4 sentences in {self._lang_label()} describing your plan",
  "steps": ["step 1", "step 2"],
  "files_to_change": [
    {{"path": "relative/path.py", "action": "create|update", "why": "why this file", "detail": "what goes in it"}}
  ],
  "tests": ["how you will verify the change"],
  "deployment": null,
  "memory": [
    {{"kind": "fact|preference|architecture|lesson|decision", "key": "short-stable-key", "content": "durable knowledge", "importance": 7, "tags": ["tag"]}}
  ]
}}

Rules for the plan:
- `files_to_change` must be real paths; prefer creating new files over rewriting working ones.
- At most {MAX_FILES_PER_PR} files, each under {MAX_FILE_BYTES} bytes.
- `deployment` stays null unless the task is explicitly about Cloudflare Worker/Pages deployment.
- `memory` entries must be things that stay true for future tasks (architecture decisions, user preferences,
  credential locations, naming conventions, gotchas). Do NOT store ephemeral status.
- If the request is unclear, dangerous, or needs credentials you do not have: set "needs_human": true and explain.
"""

    # -- stage 2: generation ------------------------------------------------
    def codegen_prompt(self, ctx: TaskContext, plan: Dict[str, Any]) -> str:
        return f"""# STAGE 2 — IMPLEMENT THE PLAN

## User request
Title: {ctx.issue_title or "(none)"}
Body:
\"\"\"
{truncate(ctx.issue_body, 4000)}
\"\"\"
{truncate(ctx.comment_body, 3000)}

## Approved plan
{json_dumps_safe(plan, 6000)}

## Rules for this stage
- Produce COMPLETE files. No `...`, no "# TODO", no placeholders, no partial diffs.
- Use the `write_file` tool once per file: {{"path": "...", "mode": "create"|"update", "content": "<full file text>"}}.
- Keep every file self-contained and runnable.
- Preserve backwards compatibility; only touch what the plan declares.
- Use the other tools when they genuinely help:
  * `read_file`  — read a file before editing it (do this for every `update`).
  * `search_repo` — find where a symbol is used before changing it.
  * `context7_docs` — fetch up-to-date library documentation before using a library API.
  * `list_cloudflare_workers` / `list_cloudflare_projects` — before any edge deployment.
  * `deploy_cloudflare` — ONLY when the task explicitly asks to deploy.
  * `ask_human` — when you are blocked or the task is risky.
- When the work is done, finish your turn with the `submit` tool:
  {{"commit_message": "feat: ...", "pr_title": "...", "pr_body": "detailed markdown description", "summary": "what changed and why", "tests": ["what was verified"], "completed": true}}
"""

    def file_write_prompt(self, ctx: TaskContext, plan: Dict[str, Any], path: str, detail: str) -> str:
        return f"""# STAGE 2b — WRITE ONE FILE

Task: {ctx.issue_title or first_line(ctx.instruction)}
Plan summary: {truncate(str(plan.get('summary', '')), 800)}

Write the complete content of: `{path}`
What it must contain: {detail or "as described by the plan"}

Call the `write_file` tool exactly once with the FULL file content, then stop.
"""

    # -- stage 3: reply / self-heal ----------------------------------------
    def reply_prompt(self, ctx: TaskContext, plan: Dict[str, Any], report: Dict[str, Any]) -> str:
        return f"""# STAGE 3 — REPORT BACK TO THE USER

## Original request
{truncate(ctx.issue_body or ctx.instruction, 2500)}

## What actually happened
{json_dumps_safe(report, 5000)}

## Plan
{json_dumps_safe(plan, 2500)}

Write the GitHub issue comment that {AGENT_NAME} will post. Markdown, in {self._lang_label()}.
Structure:
- ✅ / ⚠️ / 🛑 header line with the outcome
- 🔍 what you understood (1-2 lines)
- 🛠️ what you changed (bullet list: file → purpose)
- 🧠 what you learned and stored in memory (1-2 bullets, only if real)
- ⚠️ risks / follow-ups (only if real)
- 🔗 links: PR, commit, run (use exactly the URLs from the report; never invent one)
- 🤖 footer line: `_{AGENT_NAME} v{AGENT_VERSION} • {self.agent.brain.describe_brain()}_`

Do NOT use code fences around the whole comment. Do not mention that you are a language model.
Return only the comment markdown.
"""

    # -- helpers ------------------------------------------------------------
    def _constitution_block(self) -> str:
        """The repository's own rulebook (ABU_ALI.md), injected into every prompt."""
        text = self.agent.project_constitution()
        if not text:
            return ""
        return f"\n## Project constitution (from {INSTRUCTIONS_FILE} — these rules are binding)\n\n{text}\n"

    def _lang_label(self) -> str:
        return "Arabic" if self.cfg.language.startswith("ar") else "English"

    def _memory_block(self, ctx: TaskContext) -> str:
        if not ctx.memories:
            return "(no stored memories matched this task)"
        lines = []
        for item in ctx.memories:
            lines.append(
                f"- [{item.get('kind', 'fact')}] {item.get('content', '')}"
                f" (importance {item.get('importance', 5)}, key={item.get('key', '')})"
            )
        return "\n".join(lines)[:8000]

    def _history_block(self, ctx: TaskContext) -> str:
        if not ctx.history:
            return ""
        lines = ["## Previous interactions on this issue (Supabase)"]
        for item in reversed(ctx.history[:8]):
            lines.append(f"- ({item.get('role')}) @{item.get('author')}: {truncate(item.get('body', ''), 400)}")
        return "\n".join(lines)

    def _files_block(self, ctx: TaskContext) -> str:
        if not ctx.files:
            return "(none)"
        lines = []
        for item in ctx.files:
            lines.append(f"### {item.get('path')}\n```\n{truncate(item.get('content', ''), 4000)}\n```")
        return "\n".join(lines)[:12000]

    def _tools_block(self, ctx: TaskContext) -> str:
        if not ctx.tool_outputs:
            return "(no tools executed yet)"
        lines = []
        for name, value in ctx.tool_outputs.items():
            lines.append(f"### {name}\n```\n{truncate(json_dumps_safe(value, 4000), 4000)}\n```")
        return "\n".join(lines)[:10000]


# ===========================================================================
# 11. THE AGENT
# ===========================================================================

class Agent:
    """Orchestrates one full run: intake → plan → execute → report → persist."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.log = LOG
        self.brain = LLMEngine(cfg)
        self.github = GitHubClient(cfg)
        self.memory = MemoryStore(cfg, github=self.github)
        self.mcp = MCPClient(cfg)
        self.cloudflare = CloudflareClient(cfg)
        self.prompts = PromptBuilder(cfg, self)
        self._constitution: Optional[str] = None
        self.started_at = utcnow()
        self.metrics: Dict[str, Any] = {
            "tasks": 0, "completed": 0, "failed": 0, "skipped": 0,
            "commits": 0, "prs": 0, "deployments": 0, "comments": 0,
            "tool_calls": 0, "memories": 0,
        }
        self.fatal_errors: List[str] = []

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    def run(self, forced_issue: Optional[int] = None, forced_instruction: str = "", dry_run: bool = False) -> int:
        if dry_run:
            self.cfg.dry_run = True
        self.log.info("=" * 78)
        self.log.info("%s v%s — run %s (%s)", AGENT_NAME, AGENT_VERSION, self.cfg.run_id or "local", self.cfg.event_name or "cli")
        self.log.info("context: %s", json_dumps_safe(self.cfg.describe(), 600))
        self.log.info("=" * 78)

        exit_code = 0
        try:
            self.preflight()
            self.memory.flush_outbox()

            contexts = self.collect_tasks(forced_issue=forced_issue, forced_instruction=forced_instruction)
            if not contexts:
                self.log.info("Nothing to do — no task triggers found in this event")
                self.report_housekeeping()
                return 0

            for ctx in contexts[:MAX_ISSUES_PER_RUN]:
                try:
                    self.handle_task(ctx)
                except PermanentError as exc:
                    self.metrics["failed"] += 1
                    self.log.error("Task failed permanently: %s", exc)
                    self._report_failure(ctx, str(exc))
                except Exception as exc:
                    self.metrics["failed"] += 1
                    self.log.error("Task crashed: %s\n%s", exc, traceback.format_exc())
                    self.fatal_errors.append(f"{ctx.brief()}: {exc}")
                    self._report_failure(ctx, f"{type(exc).__name__}: {exc}")

            if len(contexts) > MAX_ISSUES_PER_RUN:
                self.log.warning(
                    "%d more task(s) were left for the next run (MAX_ISSUES_PER_RUN=%d)",
                    len(contexts) - MAX_ISSUES_PER_RUN, MAX_ISSUES_PER_RUN,
                )
        except Exception as exc:
            exit_code = 1
            self.log.critical("Run-level failure: %s\n%s", exc, traceback.format_exc())
            self.fatal_errors.append(str(exc))
        finally:
            self.finalize()

        return exit_code

    # ------------------------------------------------------------------
    # Preflight
    # ------------------------------------------------------------------
    def preflight(self) -> None:
        problems: List[str] = []
        if not self.cfg.github_token:
            problems.append("MY_GITHUB_TOKEN is missing → the agent cannot touch GitHub")
        if not self.cfg.repo_full_name:
            problems.append("GITHUB_REPOSITORY is missing → the agent does not know which repo it works on")
        if not self.brain.available:
            problems.append("Neither GROQ_API_KEY nor GEMINI_API_KEY is set → the agent has no brain")
        if not self.cfg.supabase_ready:
            problems.append("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing → memory is disabled (outbox will buffer writes)")

        for problem in problems:
            self.log.warning("PREFLIGHT: %s", problem)

        if problems and (not self.cfg.github_token or not self.brain.available):
            raise PermanentError("Preflight failed: " + "; ".join(problems))

        self.memory.ensure_schema()
        self.github.ensure_labels_exist()
        try:
            self.mcp.describe()
        except Exception as exc:
            self.log.debug("MCP describe failed: %s", exc)
        self.log.info("Preflight finished with %d warning(s)", len(problems))

    # ------------------------------------------------------------------
    # Trigger collection
    # ------------------------------------------------------------------
    def load_event(self) -> Dict[str, Any]:
        path = self.cfg.event_path
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle) or {}
            except Exception as exc:
                self.log.warning("Could not parse the GitHub event payload: %s", exc)
        return {}

    def collect_tasks(self, forced_issue: Optional[int] = None, forced_instruction: str = "") -> List[TaskContext]:
        if forced_issue is not None:
            issue = self.github.get_issue(forced_issue)
            if not issue:
                raise PermanentError(f"Issue #{forced_issue} could not be loaded")
            ctx = self._context_from_issue(issue, trigger="manual")
            ctx.instruction = forced_instruction or ctx.issue_body
            return [ctx]

        if forced_instruction:
            return [TaskContext(trigger="manual-instruction", instruction=forced_instruction,
                                issue_title=first_line(forced_instruction)[:90])]

        event = self.load_event()
        name = self.cfg.event_name or ("schedule" if not event else "")

        if name == "issues":
            action = event.get("action")
            issue = event.get("issue") or {}
            if action in ("opened", "edited", "labeled", "reopened"):
                ctx = self._context_from_issue(issue, trigger=f"issues.{action}")
                if ctx.labels or action == "opened":
                    return [ctx]
                self.log.info("Issue #%s has none of the task labels %s — ignoring", issue.get("number"), TASK_LABELS)
                return []
            self.log.info("issues.%s is not a triggering action", action)
            return []

        if name == "issue_comment":
            return self._tasks_from_comment(event)

        if name == "workflow_dispatch":
            inputs = (event.get("inputs") or {}) if isinstance(event, dict) else {}
            instruction = str(inputs.get("instruction") or inputs.get("prompt") or "").strip()
            issue_raw = inputs.get("issue_number") or inputs.get("issue")
            issue_number = None
            if issue_raw:
                try:
                    issue_number = int(str(issue_raw).strip().lstrip("#"))
                except Exception:
                    self.log.warning("workflow_dispatch: could not parse issue_number=%r", issue_raw)
            if issue_number:
                issue = self.github.get_issue(issue_number)
                if issue:
                    ctx = self._context_from_issue(issue, trigger="workflow_dispatch")
                    ctx.instruction = instruction or ctx.issue_body
                    return [ctx]
                raise PermanentError(f"workflow_dispatch: issue #{issue_number} not found")
            if instruction:
                return [TaskContext(trigger="workflow_dispatch", instruction=instruction,
                                    issue_title=first_line(instruction)[:90])]
            return [self._sweep_context(trigger="workflow_dispatch")]

        if name in ("schedule", ""):
            return [self._sweep_context(trigger="schedule")]

        if name == "push":
            self.log.info("Push event received — nothing to do unless a task label was touched")
            return []

        self.log.info("Event '%s' does not map to a task — falling back to the scheduled sweep", name)
        return [self._sweep_context(trigger=f"fallback:{name}")]

    def _tasks_from_comment(self, event: Dict[str, Any]) -> List[TaskContext]:
        comment = event.get("comment") or {}
        issue = event.get("issue") or {}
        body = comment.get("body") or ""
        author = ((comment.get("user") or {}).get("login")) or ""
        issue_number = issue.get("number")

        if not body.strip() or not issue_number:
            return []
        if self.github.is_agent_comment(comment):
            self.log.info("Ignoring our own comment on #%s", issue_number)
            return []

        command = self.parse_command(body)
        labels = [str((label or {}).get("name", "")).lower() for label in (issue.get("labels") or [])]
        is_task_issue = any(label in labels for label in TASK_LABELS + ["abu-ali"])

        if command is None and not is_task_issue:
            self.log.info("Comment on #%s is neither a command nor on a task issue — ignoring", issue_number)
            return []
        if command is not None and command.get("skip"):
            self.log.info("Comment on #%s used the opt-out marker — ignoring", issue_number)
            return []

        if command and not self.github.has_min_permission(author, self.cfg.command_min_permission):
            self.log.warning("@%s tried to command the agent but lacks '%s' permission — refusing politely",
                             author, self.cfg.command_min_permission)
            self.github.post_comment(
                issue_number,
                f"🛑 عذراً @{author} — تنفيذ الأوامر يحتاج صلاحية `{self.cfg.command_min_permission}` في هذا المستودع.\n\n"
                f"_Requesting a command requires `{self.cfg.command_min_permission}` permission._",
            )
            return []

        ctx = self._context_from_issue(issue, trigger="issue_comment")
        ctx.comment_body = body
        ctx.comment_author = author
        ctx.comment_id = comment.get("id")

        # Tool directives embedded in the comment run *before* the brain.
        if command:
            ctx.instruction = command.get("instruction") or body
            ctx.info_only = bool(command.get("info_only"))
            self._run_inline_directives(ctx, command)
        else:
            ctx.instruction = body
        self.memory.record_interaction(
            repo=self.cfg.repo_full_name, issue_number=issue_number, author=author,
            role="user", body=body, metadata={"trigger": "issue_comment", "command": bool(command)},
        )
        return [ctx]

    def parse_command(self, body: str) -> Optional[Dict[str, Any]]:
        """
        Parse `/abu ...` style commands.

        Supported:
          /abu deploy worker <name>        → deploy the Worker named <name>
          /abu deploy pages [project]      → trigger a Pages deployment
          /abu cloudflare workers|projects → list edge assets
          /abu docs <library> [topic]      → pull library docs through Context7 MCP
          /abu repo <kindle> <query>       → search the repository for a string
          /abu merge                       → merge the PR linked to this issue
          /abu status                      → report task history from Supabase
          /abu stop                        → opt-out marker (agent ignores this comment)
          /abu <anything else>             → treat as a normal instruction
        """
        lowered = (body or "").lower()
        if "<!-- abu-ali:skip -->" in lowered or "/abu stop" in lowered:
            return {"skip": True}

        match = re.search(rf"(?:^|\s){re.escape(COMMAND_PREFIX)}\b\s*(.*)", body or "", re.IGNORECASE | re.DOTALL)
        if not match:
            return None
        rest = (match.group(1) or "").strip()
        if not rest:
            return {"instruction": "قدّم ملخصاً لحالة هذا المستودع واقترح الخطوة التالية."}

        tokens = rest.split()
        head = tokens[0].lower()
        command: Dict[str, Any] = {"instruction": rest, "name": head}

        if head == "deploy" and len(tokens) >= 2:
            target = tokens[1].lower()
            command["kind"] = "deploy"
            command["target"] = target
            command["arg"] = " ".join(tokens[2:]).strip()
        elif head in ("cloudflare", "cf"):
            command["kind"] = "cloudflare"
            command["arg"] = " ".join(tokens[1:]).strip().lower() or "workers"
        elif head in ("docs", "context7"):
            command["kind"] = "docs"
            command["arg"] = " ".join(tokens[1:]).strip()
        elif head in ("search", "repo", "find"):
            command["kind"] = "search"
            command["arg"] = " ".join(tokens[1:]).strip()
        elif head == "merge":
            command["kind"] = "merge"
        elif head in ("status", "log", "history"):
            command["kind"] = "status"

        # Lookup commands answer with data, they do not rewrite the repository —
        # unless the same comment also asks for a change.
        if command.get("kind") in ("status", "cloudflare", "docs", "search"):
            command["info_only"] = not _looks_like_change_request(rest)
        return command

    def _run_inline_directives(self, ctx: TaskContext, command: Dict[str, Any]) -> None:
        """Deterministic tool runs triggered by a comment (no LLM involved)."""
        kind = command.get("kind")
        try:
            if kind == "cloudflare":
                what = command.get("arg") or "workers"
                if what.startswith("project"):
                    ctx.tool_outputs["cloudflare_projects"] = self.cloudflare.list_pages_projects()
                else:
                    ctx.tool_outputs["cloudflare_workers"] = self.cloudflare.list_workers()
                self.metrics["tool_calls"] += 1
            elif kind == "docs" and command.get("arg"):
                parts = command["arg"].split()
                docs = self.mcp.context7_docs(parts[0], " ".join(parts[1:]))
                if docs:
                    ctx.tool_outputs["context7_docs"] = docs
                self.metrics["tool_calls"] += 1
            elif kind == "search" and command.get("arg"):
                ctx.tool_outputs["search_repo"] = self.search_repo(command["arg"])
                self.metrics["tool_calls"] += 1
            elif kind == "status" and ctx.issue_number:
                ctx.tool_outputs["task_history"] = self.memory.find_task(
                    repo=self.cfg.repo_full_name, issue_number=int(ctx.issue_number), limit=5
                )
                self.metrics["tool_calls"] += 1
        except Exception as exc:
            self.log.warning("Inline directive '%s' failed: %s", kind, exc)

    def _context_from_issue(self, issue: Dict[str, Any], *, trigger: str) -> TaskContext:
        labels = [str((label or {}).get("name", "")).lower() for label in (issue.get("labels") or [])]
        author = ((issue.get("user") or {}).get("login")) or ""
        number = issue.get("number")
        return TaskContext(
            trigger=trigger,
            issue_number=int(number) if number else None,
            issue_title=issue.get("title") or "",
            issue_body=issue.get("body") or "",
            issue_author=author,
            issue_url=issue.get("html_url") or "",
            labels=labels,
            instruction=(issue.get("title") or "") + "\n" + (issue.get("body") or ""),
        )

    def _sweep_context(self, *, trigger: str) -> TaskContext:
        """The self-generated maintenance task (knowledge harvesting + hygiene)."""
        return TaskContext(
            trigger=trigger,
            is_sweep=True,
            issue_title="Agent maintenance sweep (knowledge harvest)",
            instruction=(
                "Run the periodic maintenance sweep: "
                "1) harvest durable knowledge from the current repository state and the latest agent runs into memory, "
                "2) flag anything broken or risky that a human should look at, "
                "3) do not modify any file unless something is objectively broken."
            ),
        )

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------
    def handle_task(self, ctx: TaskContext) -> None:
        self.metrics["tasks"] += 1
        task_id = self.memory.start_task(
            repo=self.cfg.repo_full_name, trigger=ctx.trigger, actor=ctx.comment_author or ctx.issue_author or self.cfg.actor,
            issue_number=ctx.issue_number, issue_title=ctx.title,
            details={"is_sweep": ctx.is_sweep, "labels": ctx.labels, "dry_run": self.cfg.dry_run},
        )
        started = time.time()

        if ctx.issue_number:
            self.github.set_labels(ctx.issue_number, add=["abu-ali"], remove=["agent-failure"])
            self._post(ctx, f"🤖 **{AGENT_NAME}** بدأ العمل على هذه المهمة…\n\n_`{ctx.trigger}` • v{AGENT_VERSION} • {self.brain.describe_brain()}_")

        # ---- assemble context -------------------------------------------
        ctx.memories = self.memory.recall(ctx.instruction + " " + ctx.issue_title, repo=self.cfg.repo_full_name)
        if ctx.issue_number:
            ctx.history = self.memory.history(repo=self.cfg.repo_full_name, issue_number=ctx.issue_number)
        self.log.info("Context ready: %d memory hit(s), %d history item(s)", len(ctx.memories), len(ctx.history))

        # ---- plan --------------------------------------------------------
        self._set_stage(task_id, "planning")
        plan = self.brain.plan_json(
            self.prompts.plan_prompt(ctx),
            system=self.prompts.system(),
            prefer="groq" if ctx.is_sweep else "gemini",
            label="plan",
        )
        plan_meta = plan.get("_meta", {})
        self.log.info("Plan [%s]: %s", plan_meta.get("model", "?"), truncate(str(plan.get("summary", "")), 400))

        if plan.get("needs_human"):
            reason = plan.get("needs_human_reason") or plan.get("summary") or "the task needs a human decision"
            self.log.warning("Plan requested human input: %s", reason)
            self._post(ctx, f"🛑 **{AGENT_NAME}** يحتاج تدخّل بشري:\n\n{reason}\n\n_أضف التفاصيل ثم أعد تشغيل المهمة._")
            if ctx.issue_number:
                self.github.set_labels(ctx.issue_number, add=["needs-human"])
            self.memory.finish_task(task_id, status="needs_human", stage="plan", summary=str(reason)[:1000],
                                    duration_ms=int((time.time() - started) * 1000))
            self.metrics["skipped"] += 1
            return

        # ---- execute -----------------------------------------------------
        self._set_stage(task_id, "executing")
        execution = self.execute_info_only(ctx, plan) if ctx.info_only else self.execute_plan(ctx, plan)
        report: Dict[str, Any] = {
            "plan_summary": plan.get("summary", ""),
            "task_type": plan.get("task_type"),
            "risk": plan.get("risk"),
            "commit_message": execution.get("commit_message"),
            "files": [item["path"] for item in execution.get("files", [])],
            "branch": execution.get("branch"),
            "commit_sha": execution.get("commit_sha"),
            "pr_url": execution.get("pr_url"),
            "pr_number": execution.get("pr_number"),
            "merged": execution.get("merged", False),
            "deployments": execution.get("deployments", []),
            "tool_calls": execution.get("tool_calls", []),
            "tests": execution.get("tests", []),
            "blocked": execution.get("blocked"),
            "notes": execution.get("summary"),
            "brain": self.brain.describe_brain(),
            "llm_usage": self.brain.usage_summary(),
            "run_url": self.cfg.run_url,
        }
        if report["commit_sha"]:
            report["commit_url"] = f"{self.cfg.server_url}/{self.cfg.repo_full_name}/commit/{report['commit_sha']}"

        # ---- communicate -------------------------------------------------
        self._set_stage(task_id, "reporting")
        comment = self.compose_comment(ctx, plan, report)
        if comment:
            self._post(ctx, comment)

        # ---- persist -----------------------------------------------------
        remember_count = self.persist_memory(ctx, plan, report)
        self.metrics["memories"] += remember_count
        status = "blocked" if execution.get("blocked") else ("completed" if execution.get("commit_sha") else "analyzed")
        self.memory.finish_task(
            task_id, status=status, stage="done",
            summary=truncate(str(report.get("notes") or plan.get("summary") or ""), 2000),
            details=report, llm_provider=(plan_meta or {}).get("provider"), llm_model=(plan_meta or {}).get("model"),
            branch=execution.get("branch"), commit_sha=execution.get("commit_sha"),
            pr_number=execution.get("pr_number"), pr_url=execution.get("pr_url"),
            duration_ms=int((time.time() - started) * 1000),
        )
        self.metrics["completed"] += 1
        self.metrics["commits"] += 1 if execution.get("commit_sha") else 0
        self.metrics["prs"] += 1 if execution.get("pr_number") else 0
        self.log.info("Task finished: %s", status)

    def _set_stage(self, task_id: Optional[int], stage: str) -> None:
        self.log.info("→ stage: %s", stage)
        self.memory.finish_task(task_id, status="running", stage=stage)

    # ------------------------------------------------------------------
    # The reasoning/acting loop
    # ------------------------------------------------------------------
    def execute_plan(self, ctx: TaskContext, plan: Dict[str, Any]) -> Dict[str, Any]:
        tools = self.build_tool_schema()
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": self.prompts.system()},
            {"role": "user", "content": self.prompts.codegen_prompt(ctx, plan)},
        ]

        files_written: Dict[str, Dict[str, str]] = {}
        tool_notes: List[Dict[str, Any]] = []
        submission: Dict[str, Any] = {}
        deploy_specs: List[Dict[str, Any]] = []
        blocked: Optional[str] = None
        deletions: List[str] = []

        for turn in range(1, MAX_TOOL_TURNS + 1):
            messages = self._trim_messages(messages)
            turn_result = self._tool_turn(messages, tools, turn)
            message = turn_result.get("message") or {}
            calls = turn_result.get("tool_calls") or []
            content = message.get("content") or ""
            if content.strip():
                self.log.debug("assistant: %s", truncate(content, 500))
            messages.append(message)

            if not calls:
                if submission:
                    break
                self.log.info("No tool calls in turn %d — nudging the model to finish with submit()", turn)
                messages.append({
                    "role": "user",
                    "content": "You did not call any tool. Continue with `write_file` calls, or finish with the `submit` tool.",
                })
                continue

            for call in calls:
                name = call.get("name") or ""
                args = call.get("arguments") or {}
                self.metrics["tool_calls"] += 1
                self.log.info("[tool] %s(%s)", name, truncate(json_dumps_safe(args, 400), 300))

                if name == "write_file":
                    path = str(args.get("path") or "").strip().lstrip("/")
                    content_value = args.get("content")
                    if not path or content_value is None:
                        result = {"ok": False, "error": "write_file needs 'path' and 'content'"}
                    elif _is_protected_path(path):
                        result = {"ok": False, "error": f"'{path}' is protected by the safety policy"}
                    else:
                        files_written[path] = {"path": path, "content": str(content_value),
                                               "action": str(args.get("mode") or "create").lower()}
                        result = {"ok": True, "path": path, "bytes": len(str(content_value)), "mode": args.get("mode", "create")}
                elif name == "read_file":
                    result = {"ok": True, "path": args.get("path"),
                              "content": truncate(self.read_repo_file(str(args.get("path") or "")) or "", 6000)}
                elif name == "search_repo":
                    result = {"ok": True, "matches": self.search_repo(str(args.get("query") or ""), limit=8)}
                elif name == "context7_docs":
                    docs = self.mcp.context7_docs(str(args.get("library") or ""), str(args.get("topic") or ""))
                    result = {"ok": bool(docs), "docs": docs or "(Context7 returned nothing)"}
                elif name == "list_cloudflare_workers":
                    result = {"ok": True, "workers": self.cloudflare.list_workers()}
                elif name == "list_cloudflare_projects":
                    result = {"ok": True, "projects": [p.get("name") for p in self.cloudflare.list_pages_projects()]}
                elif name == "deploy_cloudflare":
                    result = {"ok": True, "note": "deployment queued for after the commit", "spec": args}
                    deploy_specs.append(dict(args))
                elif name == "delete_file":
                    path = str(args.get("path") or "").strip().lstrip("/")
                    if not path or _is_protected_path(path):
                        result = {"ok": False, "error": "refusing to delete that path"}
                    elif not args.get("confirmed"):
                        result = {"ok": False, "error": "deletion requires \"confirmed\": true and a clear reason"}
                    else:
                        deletions.append(path)
                        files_written.pop(path, None)
                        result = {"ok": True, "deleted": path}
                elif name == "ask_human":
                    blocked = str(args.get("question") or args.get("reason") or "the agent needs clarification")
                    result = {"ok": True, "note": "the run will stop and ask the human"}
                elif name == "submit":
                    submission = dict(args)
                    result = {"ok": True, "note": "plan accepted, finishing"}
                else:
                    result = {"ok": False, "error": f"unknown tool '{name}'"}

                tool_notes.append({"tool": name, "args": truncate(json_dumps_safe(args, 400), 400), "result": truncate(json_dumps_safe(result, 600), 600)})
                messages.append(self._tool_result_message(call, result))

            if submission or blocked:
                break

        # Fallback: plan declared files that never got content → ask for them one by one.
        planned = plan.get("files_to_change") or []
        if not submission:
            submission = {
                "commit_message": f"feat: {first_line(ctx.issue_title or ctx.instruction) or 'agent update'}",
                "pr_title": f"[{AGENT_NAME}] {first_line(ctx.issue_title or ctx.instruction)}",
                "pr_body": f"Automated change by {AGENT_NAME} v{AGENT_VERSION}.\n\nPlan: {truncate(str(plan.get('summary','')), 3000)}",
                "summary": truncate(str(plan.get("summary", "")), 2000),
                "tests": plan.get("tests") or [],
                "completed": True,
            }
        for item in planned:
            path = str(item.get("path") or "").strip().lstrip("/")
            if not path or path in files_written or _is_protected_path(path):
                continue
            self.log.info("Missing content for planned file %s — generating it now", path)
            content_value = self._generate_file_content(ctx, plan, path, str(item.get("detail") or item.get("why") or ""))
            if content_value:
                files_written[path] = {"path": path, "content": content_value, "action": str(item.get("action") or "create")}

        result: Dict[str, Any] = {
            "files": list(files_written.values()),
            "deletions": deletions,
            "tool_calls": tool_notes[-20:],
            "commit_message": submission.get("commit_message") or f"feat: {first_line(ctx.issue_title) or 'agent update'}",
            "pr_title": submission.get("pr_title") or f"[{AGENT_NAME}] {first_line(ctx.issue_title or ctx.instruction)}",
            "pr_body": submission.get("pr_body") or "",
            "summary": submission.get("summary") or "",
            "tests": submission.get("tests") or [],
            "blocked": blocked,
            "branch": None,
            "commit_sha": None,
            "pr_number": None,
            "pr_url": None,
            "merged": False,
            "deployments": [],
        }

        if blocked:
            self.log.warning("The brain asked for human input: %s", blocked)
            return result

        # ---- apply the change -------------------------------------------
        if not result["files"] and not result["deletions"]:
            self.log.info("No file changes were produced — this run is analysis-only")
            return result

        branch = self.branch_name(ctx)
        if self.github.ensure_branch(branch) is None:
            result["notes"] = "could not create or reach the working branch"
            return result
        result["branch"] = branch

        sha = self.github.commit_files(branch, result["files"], self._commit_message(ctx, result), deletions=result["deletions"])
        result["commit_sha"] = sha
        if not sha:
            result["notes"] = "the commit step failed — see the run log"
            return result

        pr = self.github.open_pr(
            head=branch,
            title=truncate(str(result["pr_title"]), 240),
            body=self._pr_body(ctx, plan, result),
        )
        if pr:
            result["pr_number"] = pr.get("number")
            result["pr_url"] = pr.get("html_url")
            if self.cfg.auto_merge and result["pr_number"]:
                result["merged"] = self.github.merge_pr(int(result["pr_number"]))
                if result["merged"]:
                    self.log.info("Auto-merged PR #%s into %s", result["pr_number"], self.cfg.default_branch)

        # ---- deployments -------------------------------------------------
        if deploy_specs or plan.get("deployment"):
            result["deployments"] = self.run_deployments(ctx, deploy_specs, plan.get("deployment"))
            self.metrics["deployments"] += len([d for d in result["deployments"] if d.get("status") == "deployed"])

        return result

    def execute_info_only(self, ctx: TaskContext, plan: Dict[str, Any]) -> Dict[str, Any]:
        """Lookup commands (/abu status, /abu cloudflare …) answer without changing anything."""
        self.log.info("Info-only task: answering from tool output, no code generation")
        return {
            "files": [],
            "deletions": [],
            "tool_calls": [{"tool": name, "args": {}, "result": "(inline directive)"} for name in ctx.tool_outputs],
            "commit_message": None,
            "pr_title": None,
            "pr_body": None,
            "summary": truncate(str(plan.get("summary") or "تقرير معلوماتي"), 2000),
            "tests": [],
            "blocked": None,
            "branch": None,
            "commit_sha": None,
            "pr_number": None,
            "pr_url": None,
            "merged": False,
            "deployments": [],
            "notes": "info-only command — the repository was not modified",
        }

    def _tool_turn(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]], turn: int) -> Dict[str, Any]:
        """
        One assistant turn with tool calling:
          Groq (native OpenAI-style tools) → Gemini (function calling) → text fallback.
        """
        # 1) Groq
        if self.cfg.groq_api_key:
            try:
                return self._groq_tool_turn(messages, tools)
            except Exception as exc:
                self.log.warning("Groq tool turn failed (%s) — trying Gemini", exc)
        # 2) Gemini
        if self.cfg.gemini_api_key:
            try:
                return self._gemini_tool_turn(messages, tools)
            except Exception as exc:
                self.log.warning("Gemini tool turn failed (%s) — falling back to text mode", exc)
        # 3) Text fallback: ask for a JSON envelope
        return self._text_tool_turn(messages, tools)

    def _groq_tool_turn(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        payload = {
            "model": self.cfg.groq_models[0],
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_output_tokens,
        }

        def _call() -> Dict[str, Any]:
            body, status, _ = http_json(
                "POST", f"{self.cfg.groq_api_base}/chat/completions", label="groq tools",
                headers={"Authorization": f"Bearer {self.cfg.groq_api_key}", "Content-Type": "application/json"},
                json_body=payload, policy=RetryPolicy(attempts=4, base_delay=2.0, max_delay=75.0),
            )
            if status == 429:
                raise RetryableError("groq rate limited during tool turn")
            if status >= 400 or not isinstance(body, dict):
                raise PermanentError(f"groq tool turn HTTP {status}: {json_dumps_safe(body, 300)}")
            choice = ((body.get("choices") or [{}])[0]).get("message") or {}
            calls = []
            for index, call in enumerate(choice.get("tool_calls") or []):
                function = call.get("function") or {}
                arguments = safe_json_loads(function.get("arguments")) or {}
                calls.append({"id": call.get("id") or f"call_{index}", "name": function.get("name"), "arguments": arguments})
            message: Dict[str, Any] = {"role": "assistant", "content": choice.get("content") or ""}
            if calls:
                message["tool_calls"] = [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                    for c in calls
                ]
            return {"message": message, "tool_calls": calls}

        return call_with_retry("groq tool turn", _call, RetryPolicy(attempts=3, base_delay=2.0, max_delay=60.0))

    def _gemini_tool_turn(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        model = self.cfg.gemini_models[0]
        contents = self._messages_to_gemini(messages)
        declarations = [{
            "name": tool["function"]["name"],
            "description": tool["function"].get("description", ""),
            "parameters": tool["function"].get("parameters", {"type": "object", "properties": {}}),
        } for tool in tools]

        body, status, _ = http_json(
            "POST", f"{self.cfg.gemini_api_base}/models/{model}:generateContent", label="gemini tools",
            headers={"x-goog-api-key": self.cfg.gemini_api_key, "Content-Type": "application/json"},
            json_body={
                "contents": contents,
                "tools": [{"functionDeclarations": declarations}],
                "generationConfig": {"temperature": self.cfg.temperature, "maxOutputTokens": self.cfg.max_output_tokens},
            },
            policy=RetryPolicy(attempts=3, base_delay=3.0, max_delay=90.0),
            ok_status=(200,),
        )
        if status >= 400 or not isinstance(body, dict):
            raise PermanentError(f"gemini tool turn HTTP {status}: {json_dumps_safe(body, 300)}")

        calls: List[Dict[str, Any]] = []
        text_parts: List[str] = []
        for candidate in body.get("candidates") or []:
            for part in ((candidate.get("content") or {}).get("parts") or []):
                if part.get("functionCall"):
                    calls.append({
                        "id": f"call_{len(calls)}",
                        "name": part["functionCall"].get("name"),
                        "arguments": part["functionCall"].get("args") or {},
                    })
                elif part.get("text"):
                    text_parts.append(part["text"])
        message: Dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts)}
        if calls:
            message["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                for c in calls
            ]
        return {"message": message, "tool_calls": calls}

    def _text_tool_turn(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Last-resort mode: ask the model for a JSON envelope of tool calls."""
        tool_names = [tool["function"]["name"] for tool in tools]
        instruction = (
            "You have no native tool calling available. Reply with ONE JSON object:\n"
            '{"tool_calls": [{"name": "<tool>", "arguments": {...}}], "content": "short note"}\n'
            f"Available tools: {', '.join(tool_names)}\n"
            "Use the same arguments documented in the tool schema you were given."
        )
        prompt = instruction + "\n\n--- conversation so far ---\n" + truncate(
            "\n".join(f"[{m.get('role')}] {truncate(str(m.get('content') or ''), 1500)}" for m in messages), 24000
        )
        answer = self.brain.ask(prompt, prefer="gemini", json_mode=True, label="text-tools")
        parsed = extract_json_object(answer.text) or {}
        calls = []
        for index, call in enumerate(parsed.get("tool_calls") or []):
            if isinstance(call, dict) and call.get("name"):
                calls.append({"id": f"call_{index}", "name": call["name"], "arguments": call.get("arguments") or {}})
        message: Dict[str, Any] = {"role": "assistant", "content": parsed.get("content") or answer.text[:500]}
        if calls:
            message["tool_calls"] = [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                for c in calls
            ]
        return {"message": message, "tool_calls": calls}

    def _messages_to_gemini(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        contents: List[Dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                contents.append({"role": "user", "parts": [{"text": "[SYSTEM INSTRUCTIONS]\n" + str(message.get("content") or "")}]})
                continue
            if role == "assistant":
                parts: List[Dict[str, Any]] = []
                if message.get("content"):
                    parts.append({"text": str(message["content"])})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    parts.append({"functionCall": {"name": function.get("name"),
                                                  "args": safe_json_loads(function.get("arguments")) or {}}})
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue
            if role == "tool":
                name = message.get("name") or "tool"
                payload = safe_json_loads(message.get("content")) or {"raw": message.get("content")}
                contents.append({"role": "user", "parts": [{"functionResponse": {"name": name, "response": {"result": payload}}}]})
                continue
            contents.append({"role": "user", "parts": [{"text": str(message.get("content") or "")}]})
        return contents

    def _trim_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Keep the conversation inside the context budget.

        The system prompt and the original task prompt are always kept; older
        tool chatter is dropped from the middle, oldest first, and a marker is
        inserted so the model knows history was compacted.
        """
        def size(items: List[Dict[str, Any]]) -> int:
            return sum(len(str(item.get("content") or "")) for item in items)

        if size(messages) <= MAX_CONTEXT_CHARS or len(messages) <= 4:
            return messages

        compacted = messages[:2]
        tail = messages[2:]
        dropped = 0
        while tail and size(compacted + tail) > MAX_CONTEXT_CHARS:
            tail = tail[1:]
            dropped += 1
        if dropped:
            self.log.info("Context guard: dropped %d older message(s) to stay within the budget", dropped)
            compacted.append({
                "role": "user",
                "content": f"[{dropped} older tool exchanges were compacted to save context. "
                           f"Everything you wrote to disk is already recorded.]",
            })
        return compacted + tail

    def _tool_result_message(self, call: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": call.get("id") or "call_0",
            "name": call.get("name") or "tool",
            "content": truncate(json_dumps_safe(result, 8000), 8000),
        }

    def _generate_file_content(self, ctx: TaskContext, plan: Dict[str, Any], path: str, detail: str) -> str:
        """Second pass used when the plan listed a file but no content was produced."""
        try:
            answer = self.brain.ask(
                self.prompts.file_write_prompt(ctx, plan, path, detail),
                system=self.prompts.system(), prefer="gemini", label=f"file:{path}",
            )
        except Exception as exc:
            self.log.warning("Content generation for %s failed: %s", path, exc)
            return ""
        text = answer.text or ""
        parsed = extract_json_object(text)
        if isinstance(parsed, dict):
            for key in ("content", "file_content", "text", "body"):
                if isinstance(parsed.get(key), str) and parsed[key].strip():
                    return parsed[key]
            for call in parsed.get("tool_calls") or []:
                if isinstance(call, dict):
                    arguments = call.get("arguments") or {}
                    if isinstance(arguments, dict) and isinstance(arguments.get("content"), str):
                        return arguments["content"]
        # Strip a single wrapping code fence if present.
        fenced = re.match(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*)\n```\s*$", text, re.DOTALL)
        if fenced:
            return fenced.group(1)
        return text if text.strip() and not text.strip().startswith("{") else ""

    # ------------------------------------------------------------------
    # Tools the brain can call
    # ------------------------------------------------------------------
    def build_tool_schema(self) -> List[Dict[str, Any]]:
        def fn(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
            return {"type": "function", "function": {"name": name, "description": description,
                                                     "parameters": {"type": "object", "properties": properties, "required": required}}}

        tools = [
            fn("write_file", "Write the COMPLETE content of one file in the repository.",
               {"path": {"type": "string", "description": "Repository-relative path, e.g. src/app.py"},
                "content": {"type": "string", "description": "The full file content. Never truncated."},
                "mode": {"type": "string", "enum": ["create", "update"], "description": "create for new files, update for existing ones"}},
               ["path", "content"]),
            fn("read_file", "Read a file from the repository before editing it.",
               {"path": {"type": "string"}}, ["path"]),
            fn("search_repo", "Search the repository for a string to find where something is defined or used.",
               {"query": {"type": "string"}}, ["query"]),
            fn("context7_docs", "Fetch up-to-date documentation for a library through the Context7 MCP server.",
               {"library": {"type": "string", "description": "e.g. 'supabase', 'cloudflare workers', 'next.js'"},
                "topic": {"type": "string"}}, ["library"]),
            fn("list_cloudflare_workers", "List the Cloudflare Workers in the account.", {}, []),
            fn("list_cloudflare_projects", "List the Cloudflare Pages projects in the account.", {}, []),
            fn("deploy_cloudflare", "Deploy to Cloudflare (Workers or Pages). Executed after the commit.",
               {"type": {"type": "string", "enum": ["worker", "pages"]},
                "name": {"type": "string", "description": "Worker name (for type=worker)"},
                "script": {"type": "string", "description": "Full Worker module source (for type=worker)"},
                "project": {"type": "string", "description": "Pages project name (for type=pages)"},
                "branch": {"type": "string"},
                "compatibility_date": {"type": "string"},
                "compatibility_flags": {"type": "array", "items": {"type": "string"}}},
               ["type"]),
            fn("delete_file", "Delete a file. Requires confirmed=true and is refused for protected paths.",
               {"path": {"type": "string"}, "confirmed": {"type": "boolean"}, "reason": {"type": "string"}},
               ["path", "confirmed"]),
            fn("ask_human", "Stop and ask the human when the task is ambiguous or risky.",
               {"question": {"type": "string"}}, ["question"]),
            fn("submit", "Finish the work and describe what should be committed.",
               {"commit_message": {"type": "string"}, "pr_title": {"type": "string"},
                "pr_body": {"type": "string"}, "summary": {"type": "string"},
                "tests": {"type": "array", "items": {"type": "string"}}, "completed": {"type": "boolean"}},
               ["commit_message"]),
        ]
        if not self.cloudflare.available:
            tools = [tool for tool in tools if not tool["function"]["name"].startswith("list_cloudflare")
                     and tool["function"]["name"] != "deploy_cloudflare"]
        return tools

    # ------------------------------------------------------------------
    # Repository access helpers
    # ------------------------------------------------------------------
    def read_repo_file(self, path: str, ref: Optional[str] = None) -> Optional[str]:
        if not path:
            return None
        local = Path(path)
        if local.exists() and local.is_file():
            try:
                return local.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                self.log.debug("local read failed for %s: %s", path, exc)
        return self.github.get_file_text(path, ref=ref)

    def search_repo(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        """Local grep first (the checkout is right here), GitHub code search second."""
        query = (query or "").strip()
        if len(query) < 2:
            return []
        matches: List[Dict[str, Any]] = []
        try:
            for path in Path(".").rglob("*"):
                if not path.is_file() or any(part in {".git", "node_modules", ".venv", "venv", "__pycache__"} for part in path.parts):
                    continue
                if path.stat().st_size > 400_000:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                if query.lower() not in text.lower():
                    continue
                for number, line in enumerate(text.splitlines(), start=1):
                    if query.lower() in line.lower():
                        matches.append({"path": str(path), "line": number, "text": truncate(line.strip(), 200)})
                        break
                if len(matches) >= limit:
                    break
        except Exception as exc:
            self.log.debug("local search failed: %s", exc)
        if matches:
            return matches
        try:
            body, status, _ = http_json(
                "GET", f"{self.cfg.api_base}/search/code", label="gh code search",
                headers=self.github.headers,
                params={"q": f"{query} repo:{self.cfg.repo_full_name}", "per_page": str(limit)},
                ok_status=(200,), allow_404=True,
            )
            if status == 200 and isinstance(body, dict):
                for item in body.get("items") or []:
                    matches.append({"path": item.get("path"), "repo": (item.get("repository") or {}).get("full_name")})
        except Exception as exc:
            self.log.debug("GitHub code search failed: %s", exc)
        return matches[:limit]

    def project_constitution(self) -> str:
        """Read the repo rulebook (ABU_ALI.md) once and cache it for the run."""
        cached = getattr(self, "_constitution", None)
        if cached is not None:
            return cached
        text = ""
        try:
            if os.path.exists(INSTRUCTIONS_FILE):
                text = Path(INSTRUCTIONS_FILE).read_text(encoding="utf-8", errors="replace")
            elif INSTRUCTIONS_FILE != "ABU_ALI.md" and os.path.exists("ABU_ALI.md"):
                text = Path("ABU_ALI.md").read_text(encoding="utf-8", errors="replace")
            else:
                text = self.github.get_file_text(INSTRUCTIONS_FILE, ref=self.cfg.default_branch) or ""
        except Exception as exc:
            self.log.debug("could not load %s: %s", INSTRUCTIONS_FILE, exc)
            text = ""
        self._constitution = truncate(text.strip(), 14000) if text.strip() else ""
        if self._constitution:
            self.log.info("Project constitution loaded from %s (%d chars)", INSTRUCTIONS_FILE, len(self._constitution))
        return self._constitution

    def build_repo_map(self, limit: int = 220) -> str:
        """A compact map of the repository: the checkout, or the Git tree as fallback."""
        entries: List[str] = []
        try:
            for path in sorted(Path(".").rglob("*")):
                if any(part in {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next"} for part in path.parts):
                    continue
                relative = str(path)
                if relative.startswith("./"):
                    relative = relative[2:]
                if not relative or relative.startswith(".abu-ali/"):
                    continue
                if path.is_dir():
                    continue
                size = 0
                try:
                    size = path.stat().st_size
                except Exception:
                    pass
                entries.append(f"{relative} ({size}B)")
                if len(entries) >= limit:
                    break
        except Exception as exc:
            self.log.debug("repo map from the filesystem failed: %s", exc)
        if not entries:
            for item in self.github.list_tree():
                if item.get("type") == "blob":
                    entries.append(f"{item.get('path')} ({item.get('size', 0)}B)")
                if len(entries) >= limit:
                    break
        if not entries:
            return "(the repository appears to be empty)"
        header = f"{len(entries)} file(s), showing up to {limit}:\n"
        return header + "\n".join(f"- {line}" for line in entries)

    # ------------------------------------------------------------------
    # Deployment
    # ------------------------------------------------------------------
    def run_deployments(self, ctx: TaskContext, specs: List[Dict[str, Any]], plan_deployment: Any) -> List[Dict[str, Any]]:
        if not self.cfg.auto_deploy:
            self.log.info("AUTO_DEPLOY is off — skipping %d deployment(s)", len(specs) + (1 if plan_deployment else 0))
            return [{"status": "skipped", "reason": "AUTO_DEPLOY=false"}]
        if not self.cloudflare.available:
            self.log.warning("Cloudflare credentials are not configured — deployments are skipped")
            return [{"status": "skipped", "reason": "cloudflare credentials missing"}]

        queue: List[Dict[str, Any]] = []
        if isinstance(plan_deployment, dict) and plan_deployment:
            queue.append(plan_deployment)
        queue.extend(specs)

        results: List[Dict[str, Any]] = []
        for spec in queue[:3]:
            outcome = self.cloudflare.deploy(spec)
            results.append(outcome)
            self.memory.log_deployment(
                repo=self.cfg.repo_full_name,
                target_type=outcome.get("target_type") or spec.get("type"),
                target_name=outcome.get("target") or spec.get("name") or spec.get("project"),
                status=outcome.get("status"),
                url=outcome.get("url"),
                commit_sha=None,
                issue_number=ctx.issue_number,
                details={"spec_keys": sorted(spec.keys())},
                error=outcome.get("reason"),
            )
            self.log.info("Deployment result: %s", json_dumps_safe({k: v for k, v in outcome.items() if k != "result"}, 300))
        return results

    # ------------------------------------------------------------------
    # Communication & memory
    # ------------------------------------------------------------------
    def compose_comment(self, ctx: TaskContext, plan: Dict[str, Any], report: Dict[str, Any]) -> str:
        try:
            answer = self.brain.ask(
                self.prompts.reply_prompt(ctx, plan, report),
                system=self.prompts.system(), prefer="groq", label="reply",
                validate=lambda candidate: len((candidate.text or "").strip()) > 40,
            )
            comment = (answer.text or "").strip()
            if comment:
                return truncate(comment, MAX_COMMENT_CHARS)
        except Exception as exc:
            self.log.warning("Reply generation failed (%s) — using the deterministic template", exc)
        return self.template_comment(ctx, plan, report)

    def template_comment(self, ctx: TaskContext, plan: Dict[str, Any], report: Dict[str, Any]) -> str:
        if report.get("blocked"):
            header = "🛑 **يحتاج تدخّل بشري**"
        elif report.get("commit_sha"):
            header = "✅ **تم التنفيذ ورفع التغييرات**"
        else:
            header = "🔍 **تم التحليل (بدون تغييرات في الكود)**"

        lines = [header, "", f"**الفهم:** {truncate(str(report.get('plan_summary') or plan.get('summary') or ''), 800)}", ""]
        files = report.get("files") or []
        if files:
            lines.append("**الملفات:**")
            lines.extend(f"- `{path}`" for path in files)
            lines.append("")
        if report.get("branch"):
            lines.append(f"**الفرع:** `{report['branch']}`")
        if report.get("commit_sha"):
            lines.append(f"**الكوميت:** [`{str(report['commit_sha'])[:8]}`]({report.get('commit_url', '')})")
        if report.get("pr_url"):
            lines.append(f"**الطلب (PR):** [#{report.get('pr_number')}]({report['pr_url']})")
        if report.get("merged"):
            lines.append("**الحالة:** تم الدمج تلقائياً ✔")
        for deployment in report.get("deployments") or []:
            lines.append(f"**النشر:** `{deployment.get('target', '')}` → {deployment.get('status')}"
                         + (f" ({deployment.get('url')})" if deployment.get("url") else ""))
        if report.get("blocked"):
            lines.append("")
            lines.append(f"**السؤال:** {report['blocked']}")
        if report.get("notes"):
            lines.append("")
            lines.append(f"**ملاحظات:** {truncate(str(report['notes']), 600)}")
        lines.append("")
        lines.append(f"_🤖 {AGENT_NAME} v{AGENT_VERSION} • {self.brain.describe_brain()}_")
        return "\n".join(lines)

    def persist_memory(self, ctx: TaskContext, plan: Dict[str, Any], report: Dict[str, Any]) -> int:
        entries: List[Dict[str, Any]] = []
        raw_memory = plan.get("memory")
        if isinstance(raw_memory, list):
            entries.extend([item for item in raw_memory if isinstance(item, dict)])

        if report.get("commit_sha") and report.get("files"):
            entries.append({
                "kind": "decision",
                "key": f"issue-{ctx.issue_number or 'manual'}-{sha1_hex(str(report['commit_sha']))[:8]}",
                "content": (
                    f"Task '{truncate(ctx.title, 120)}' was implemented on branch {report.get('branch')} "
                    f"(commit {str(report['commit_sha'])[:8]}) touching: {', '.join(report['files'][:8])}."
                ),
                "importance": 5,
                "tags": ["change", "history"],
            })
        for error in self.fatal_errors[-3:]:
            entries.append({
                "kind": "lesson",
                "key": f"failure-{sha1_hex(error)[:10]}",
                "content": f"A previous run failed with: {truncate(error, 500)}",
                "importance": 6,
                "tags": ["failure", "ops"],
            })
        if not entries:
            return 0
        stored = self.memory.remember(entries, repo=self.cfg.repo_full_name, source=ctx.brief())
        self.memory.record_interaction(
            repo=self.cfg.repo_full_name, issue_number=ctx.issue_number, author=AGENT_HANDLE, role="assistant",
            body=truncate(str(report.get("notes") or report.get("plan_summary") or ""), 8000),
            metadata={"status": "completed" if report.get("commit_sha") else "analyzed"},
        )
        return stored

    def _post(self, ctx: TaskContext, body: str) -> None:
        if not body:
            return
        if ctx.issue_number:
            if self.github.post_comment(int(ctx.issue_number), body):
                self.metrics["comments"] += 1
        else:
            self.log.info("(no issue to post to) %s", truncate(body, 1000))

    def _report_failure(self, ctx: TaskContext, error: str) -> None:
        if ctx.issue_number:
            self.github.set_labels(int(ctx.issue_number), add=["agent-failure"])
        self._post(
            ctx,
            f"🛑 **{AGENT_NAME} فشل في إكمال المهمة**\n\n"
            f"```\n{truncate(error, 1200)}\n```\n\n"
            f"🔗 السجل الكامل: {self.cfg.run_url or 'انظر صفحة Actions'}\n\n"
            f"_اكتب `{COMMAND_PREFIX} أعد المحاولة` بعد إصلاح السبب، أو أضف تعليقاً جديداً بالأمر المطلوب._",
        )

    def _commit_message(self, ctx: TaskContext, result: Dict[str, Any]) -> str:
        base = str(result.get("commit_message") or "").strip() or f"feat: {first_line(ctx.issue_title) or 'agent update'}"
        trailer = f"\n\n[skip ci]\n\nRefs: #{ctx.issue_number}" if ctx.issue_number else "\n\n[skip ci]"
        trailer += f"\nAgent: {AGENT_NAME} v{AGENT_VERSION}"
        return truncate(base + trailer, 3800)

    def _pr_body(self, ctx: TaskContext, plan: Dict[str, Any], result: Dict[str, Any]) -> str:
        lines = [
            f"## 🤖 {AGENT_NAME} — automated change",
            "",
            f"**Trigger:** `{ctx.trigger}`" + (f" • issue #{ctx.issue_number}" if ctx.issue_number else ""),
            f"**Brain:** {self.brain.describe_brain()}",
            "",
            "### What was asked",
            truncate(ctx.issue_body or ctx.instruction, 2500) or "(no description)",
            "",
            "### Plan",
            truncate(str(plan.get("summary") or ""), 2000),
            "",
            "### Files",
        ]
        lines.extend(f"- `{item['path']}` ({item.get('action', 'create')})" for item in result.get("files") or [])
        if result.get("deletions"):
            lines.extend(["", "### Deleted", *[f"- `{path}`" for path in result["deletions"]]])
        if result.get("summary"):
            lines.extend(["", "### Agent summary", truncate(str(result["summary"]), 2000)])
        if result.get("tests"):
            lines.extend(["", "### Verification", *[f"- {test}" for test in result["tests"]]])
        if result.get("deployments"):
            lines.extend(["", "### Deployments"])
            lines.extend(f"- {d.get('target', '?')} → {d.get('status')} {d.get('url') or ''}" for d in result["deployments"])
        if self.cfg.run_url:
            lines.extend(["", f"**Run:** {self.cfg.run_url}"])
        lines.extend(["", f"_{AGENT_NAME} v{AGENT_VERSION}_"])
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Wrapping up
    # ------------------------------------------------------------------
    def branch_name(self, ctx: TaskContext) -> str:
        stamp = utcnow().strftime("%Y%m%d-%H%M")
        if ctx.issue_number:
            return f"abu-ali/issue-{ctx.issue_number}-{slugify(ctx.title, 32)}"
        return f"abu-ali/{stamp}-{slugify(ctx.title, 32)}"

    def report_housekeeping(self) -> None:
        """On schedule/workflow_dispatch runs with nothing to do, keep memory fresh."""
        try:
            tasks = self.memory.recent_tasks(repo=self.cfg.repo_full_name, limit=10)
            stored = self.memory.remember([{
                "kind": "status",
                "key": "last-sweep",
                "content": (
                    f"Scheduled sweep at {now_iso()}: no open tasks. "
                    f"Recent task statuses: {', '.join(str(t.get('status')) for t in tasks[:6]) or 'none'}."
                ),
                "importance": 3,
                "tags": ["sweep", "status"],
            }], repo=self.cfg.repo_full_name, source="sweep")
            self.metrics["memories"] += stored
        except Exception as exc:
            self.log.debug("housekeeping failed: %s", exc)

    def finalize(self) -> None:
        duration = int((utcnow() - self.started_at).total_seconds())
        summary = {
            "agent": f"{AGENT_NAME} v{AGENT_VERSION}",
            "duration_s": duration,
            "metrics": self.metrics,
            "llm": self.brain.usage_summary(),
            "fatal_errors": self.fatal_errors[-5:],
            "event": self.cfg.event_name,
            "run_url": self.cfg.run_url,
        }
        self.log.info("=" * 78)
        self.log.info("RUN SUMMARY: %s", json_dumps_safe(summary, 2000))
        self.log.info("=" * 78)

        # GitHub job summary (renders inside the Actions UI)
        summary_path = os.getenv("GITHUB_STEP_SUMMARY")
        if summary_path:
            try:
                markdown = [
                    f"# 🤖 {AGENT_NAME} v{AGENT_VERSION}",
                    "",
                    f"**Event:** `{self.cfg.event_name or 'cli'}` • **Duration:** {duration}s • "
                    f"**Brain:** {self.brain.describe_brain()}",
                    "",
                    "| Metric | Value |",
                    "| --- | --- |",
                ]
                markdown.extend(f"| {key} | {value} |" for key, value in self.metrics.items())
                usage = self.brain.usage_summary()
                markdown.extend([
                    "",
                    f"**LLM calls:** {usage['calls']} • **tokens in/out:** {usage['tokens_in']}/{usage['tokens_out']}",
                ])
                if self.fatal_errors:
                    markdown.extend(["", "## Errors", *[f"- `{truncate(err, 300)}`" for err in self.fatal_errors[-5:]]])
                if self.cfg.run_url:
                    markdown.extend(["", f"[Open the run]({self.cfg.run_url})"])
                with open(summary_path, "a", encoding="utf-8") as handle:
                    handle.write("\n".join(markdown) + "\n")
            except Exception as exc:
                self.log.debug("could not write the job summary: %s", exc)

        # Persist the run log as a build artifact and sync the outbox.
        try:
            if logging.getLogger().handlers:
                for handler in logging.getLogger().handlers:
                    handler.flush()
        except Exception:
            pass
        try:
            self.memory.flush_outbox()
        except Exception as exc:
            self.log.debug("final outbox flush failed: %s", exc)


# ===========================================================================
# 12. SELF-TEST (offline, no network) & CLI
# ===========================================================================

def selftest() -> int:
    """Validate the engine offline: parsing, policy, prompts, schema, tools."""
    try:
        return _selftest_checks()
    except Exception as exc:  # a broken check must be reported, not swallowed
        print("\n=== RESULT: SELF TEST CRASHED ✘ ===")
        traceback.print_exc()
        print(f"\n{type(exc).__name__}: {exc}\n")
        return 1


def _selftest_checks() -> int:
    print(f"\n=== {AGENT_NAME} v{AGENT_VERSION} — SELF TEST ===\n")
    failures: List[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    # --- JSON extraction ---------------------------------------------------
    check("json: fenced object", extract_json_object('```json\n{"a": 1}\n```') == {"a": 1})
    check("json: prose around object", (extract_json_object('Sure! {"b": [1,2],} done') or {}).get("b") == [1, 2])
    check("json: garbage returns None", extract_json_object("no json at all") is None)
    check("json: double encoded", (extract_json_object('{"x": "{\\"y\\": 3}"}') or {}).get("x") is not None)

    # --- command parsing ---------------------------------------------------
    agent = Agent.__new__(Agent)  # no network, only pure helpers
    agent.cfg = Config()
    agent.log = LOG
    parse = Agent.parse_command(agent, "/abu deploy worker my-edge-api")
    check("command: deploy worker", bool(parse and parse.get("kind") == "deploy" and parse.get("target") == "worker"))
    parse = Agent.parse_command(agent, "شكراً!\n/abu docs supabase rls\n")
    check("command: docs", bool(parse and parse.get("kind") == "docs" and "supabase" in (parse.get("arg") or "")))
    check("command: stop marker", (Agent.parse_command(agent, "/abu stop") or {}).get("skip") is True)
    check("command: none", Agent.parse_command(agent, "just a normal comment") is None)
    check("command: plain instruction", (Agent.parse_command(agent, "/abu أضف صفحة تسجيل") or {}).get("kind") is None)

    # --- policy ------------------------------------------------------------
    check("policy: blocks .github/workflows", _is_protected_path(".github/workflows/x.yml"))
    check("policy: blocks .env", _is_protected_path(".env") and _is_protected_path("app/.env.local"))
    check("policy: allows src/app.py", not _is_protected_path("src/app.py"))

    # --- text helpers ------------------------------------------------------
    check("slugify: arabic + ascii", slugify("أضف صفحة Login") != "" and slugify("Hello World!") == "hello-world")
    check("truncate", truncate("x" * 100, 20).endswith("[truncated]"))
    check("chunk_text", len(chunk_text("abcdef", 3)) == 2)
    check("duration parser", _duration_to_seconds("1m30s") == 90.0)

    # --- config ------------------------------------------------------------
    cfg = Config.from_env()
    check("config: provider order", cfg.provider_order[:2] == ["groq", "gemini"], str(cfg.provider_order))
    check("config: describe() has no secrets", "key" not in json.dumps(cfg.describe()).lower().replace("github_token", ""))

    # --- schema & prompts --------------------------------------------------
    check("schema: has all four tables", all(t in SCHEMA_SQL for t in ("tasks_log", "system_memory", "interactions", "deployments")))
    check("schema: no DROP of agent tables", "drop table" not in SCHEMA_SQL.lower())
    ddl = _ddl_for(["tasks_log"])
    check("ddl extraction", "create table if not exists public.tasks_log" in ddl.lower())

    brain = LLMEngine.__new__(LLMEngine)
    brain.cfg = cfg
    brain.log = LOG
    brain.usage = []
    classifier = Agent.__new__(Agent)          # pure-helper instance: no network clients
    classifier.cfg = cfg
    classifier.log = LOG
    classifier._constitution = None
    classifier.brain = brain
    classifier.metrics = {"tool_calls": 0}
    classifier.cloudflare = CloudflareClient(cfg)
    classifier.github = GitHubClient(cfg)
    prompts = PromptBuilder(cfg, classifier)
    system_prompt = prompts.system()
    check("prompt: identity present", AGENT_NAME in system_prompt and "NEVER delete" in system_prompt)
    check("prompt: memory rules present", "system_memory" in system_prompt or "memory" in system_prompt.lower())
    tool_schema = Agent.build_tool_schema(classifier)
    names = {tool["function"]["name"] for tool in tool_schema}
    check("tools: core set present", {"write_file", "read_file", "search_repo", "submit", "ask_human"} <= names,
          str(sorted(names)))

    # --- dataclasses -------------------------------------------------------
    task = TaskContext(trigger="test", issue_number=7, issue_title="Add health check")
    check("TaskContext: brief", "issue=#7" in task.brief())

    print(f"\n=== RESULT: {'ALL CHECKS PASSED ✔' if not failures else f'{len(failures)} FAILED ✘'} ===")
    for failure in failures:
        print(f"  ✘ {failure}")
    print()
    return 0 if not failures else 1


def status_report() -> int:
    cfg = Config.from_env()
    print(f"""
{AGENT_NAME} v{AGENT_VERSION} — status
------------------------------------------------------------
repo            : {cfg.repo_full_name or '(unknown)'}
default branch  : {cfg.default_branch}
event           : {cfg.event_name or '(cli)'} / run {cfg.run_id or '-'}
github token    : {'configured' if cfg.github_token else 'MISSING'}
supabase        : {'configured' if cfg.supabase_ready else 'MISSING'}
groq            : {'configured' if cfg.groq_api_key else 'missing'}
gemini          : {'configured' if cfg.gemini_api_key else 'missing'}
cloudflare      : {'configured' if cfg.cloudflare_ready else 'missing'}
mcp servers     : {', '.join(sorted(cfg.mcp_servers.keys())) or 'none'}
auto deploy     : {cfg.auto_deploy}
auto merge      : {cfg.auto_merge}
dry run         : {cfg.dry_run}
outbox          : {OUTBOX_PATH} ({'present' if os.path.exists(OUTBOX_PATH) else 'empty'})
log file        : {LOG_FILE}
------------------------------------------------------------
""")
    return 0


def _optional_int(value: str) -> Optional[int]:
    """argparse helper: '' / '#42' / '42' → None / 42 / 42 (never crashes CI)."""
    cleaned = str(value or "").strip().lstrip("#")
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an issue number, got {value!r}") from None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="abu-ali", description=f"{AGENT_NAME} — Autonomous AI DevOps Architect")
    parser.add_argument("--issue", type=_optional_int, default=None,
                        help="force work on a specific issue number (empty values are ignored)")
    parser.add_argument("--instruction", type=str, default="", help="force a raw instruction")
    parser.add_argument("--dry-run", action="store_true", help="plan and print without applying anything")
    parser.add_argument("--selftest", action="store_true", help="run offline validation and exit")
    parser.add_argument("--status", action="store_true", help="print configuration status and exit")
    parser.add_argument("--quiet", action="store_true", help="console logging off (file only)")
    args = parser.parse_args(argv)

    if args.selftest:
        setup_logging(verbose=False)
        return selftest()
    setup_logging(verbose=not args.quiet)
    if args.status:
        return status_report()

    cfg = Config.from_env()
    if args.dry_run:
        cfg.dry_run = True
    LOG.info("%s booting — python %s", AGENT_NAME, sys.version.split()[0])
    LOG.info("secrets present: %s", json_dumps_safe(
        {k: v for k, v in cfg.describe().items() if k in ("supabase", "groq", "gemini", "github_token", "cloudflare")}, 300))

    agent = Agent(cfg)
    return agent.run(forced_issue=args.issue, forced_instruction=args.instruction, dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] interrupted by the operator")
        sys.exit(130)
    except Exception as unhandled:  # pragma: no cover - last line of defence
        logging.getLogger("abu-ali").critical("UNHANDLED: %s\n%s", unhandled, traceback.format_exc())
        sys.exit(1)
