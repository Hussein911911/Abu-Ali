/**
 * =============================================================================
 *  Abu Ali — J.A.R.V.I.S. dashboard on Cloudflare Workers
 * =============================================================================
 *  Serves the HUD (jarvis/index.html) and the JSON API it consumes, keeping
 *  every credential on the server side. Nothing secret ever reaches the
 *  browser.
 *
 *  Routes
 *    GET  /                      → the interface
 *    GET  /api/health            → liveness
 *    GET  /api/state             → aggregated payload for the HUD (cached 15s)
 *    POST /api/command           → dispatch the Abu Ali workflow (real run)
 *    POST /api/install-workflow  → write .github/workflows/abu_ali_agent.yml
 *
 *  Secrets (wrangler secret put …)
 *    MY_GITHUB_TOKEN, SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY,
 *    JARVIS_ACCESS_KEY (optional gate for /api/*)
 *
 *  Vars (wrangler.toml [vars])
 *    GITHUB_REPOSITORY, ABU_WORKFLOW_FILE, ABU_DEFAULT_BRANCH, TASK_LABELS
 *
 *  Deploy:  cd jarvis && npx wrangler deploy
 * =============================================================================
 */

import INDEX_HTML from "./index.html";
import WORKFLOW_YML from "../deploy/abu_ali_agent.yml";

const JSON_HEADERS = {
  "Content-Type": "application/json; charset=utf-8",
  "Cache-Control": "no-store",
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
};

const CACHE_TTL_SECONDS = 15;
const WORKFLOW_TARGET = ".github/workflows/abu_ali_agent.yml";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: { Allow: "GET, POST, HEAD, OPTIONS" } });
    }

    try {
      if (url.pathname === "/" || url.pathname === "/index.html") {
        return new Response(INDEX_HTML, {
          headers: {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "SAMEORIGIN",
          },
        });
      }

      if (url.pathname === "/favicon.ico") {
        return new Response(null, { status: 204 });
      }

      if (url.pathname.startsWith("/api/")) {
        const denied = authorise(request, env);
        if (denied) return denied;

        if (url.pathname === "/api/health") {
          return json({ ok: true, mode: env.MY_GITHUB_TOKEN ? "live" : "demo", repo: repo(env), at: new Date().toISOString() });
        }
        if (url.pathname === "/api/state" && (request.method === "GET" || request.method === "HEAD")) {
          return stateResponse(request, env, ctx);
        }
        if (url.pathname === "/api/command" && request.method === "POST") {
          return json(await dispatchCommand(env, await readJson(request)));
        }
        if (url.pathname === "/api/install-workflow" && request.method === "POST") {
          return json(await installWorkflow(env));
        }
        return json({ ok: false, message: `unknown route ${url.pathname}` }, 404);
      }

      return new Response("Not found", { status: 404, headers: { "Content-Type": "text/plain; charset=utf-8" } });
    } catch (error) {
      return json({ ok: false, message: `internal error: ${error.message}` }, 500);
    }
  },
};

/* --------------------------------------------------------------------------
 *  helpers
 * ------------------------------------------------------------------------ */

function json(payload, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: JSON_HEADERS });
}

async function readJson(request) {
  try {
    const parsed = await request.json();
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function repo(env) {
  return String(env.GITHUB_REPOSITORY || "").trim();
}

function workflowFile(env) {
  return String(env.ABU_WORKFLOW_FILE || "abu_ali_agent.yml");
}

function defaultBranch(env) {
  return String(env.ABU_DEFAULT_BRANCH || "main");
}

/** Optional shared-secret gate: set JARVIS_ACCESS_KEY and pass ?key= or x-jarvis-key. */
function authorise(request, env) {
  const expected = env.JARVIS_ACCESS_KEY;
  if (!expected) return null;
  const url = new URL(request.url);
  const supplied = request.headers.get("x-jarvis-key") || url.searchParams.get("key") || "";
  if (supplied === expected) return null;
  return json({ ok: false, message: "unauthorised — a valid ?key= or x-jarvis-key header is required" }, 401);
}

function githubHeaders(env) {
  return {
    Authorization: `Bearer ${env.MY_GITHUB_TOKEN}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "abu-ali-jarvis",
  };
}

function supabaseHeaders(env) {
  const key = env.SUPABASE_SERVICE_ROLE_KEY;
  return { apikey: key, Authorization: `Bearer ${key}`, "User-Agent": "abu-ali-jarvis" };
}

async function supabaseRows(env, table, query, limit = 20) {
  if (!env.SUPABASE_URL || !env.SUPABASE_SERVICE_ROLE_KEY) return [];
  const endpoint = `${String(env.SUPABASE_URL).replace(/\/$/, "")}/rest/v1/${table}?${query}&limit=${limit}`;
  try {
    const response = await fetch(endpoint, { headers: supabaseHeaders(env) });
    if (!response.ok) return [];
    const rows = await response.json();
    return Array.isArray(rows) ? rows : [];
  } catch {
    return [];
  }
}

async function githubJson(env, path, init = {}) {
  const response = await fetch(`https://api.github.com${path}`, {
    ...init,
    headers: { ...githubHeaders(env), ...(init.body ? { "Content-Type": "application/json" } : {}), ...(init.headers || {}) },
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  return { status: response.status, body };
}

/* --------------------------------------------------------------------------
 *  state
 * ------------------------------------------------------------------------ */

async function stateResponse(request, env, ctx) {
  const cache = caches.default;
  const cacheKey = new Request(`${new URL(request.url).origin}/api/state:${repo(env) || "demo"}`, { method: "GET" });

  const hit = await cache.match(cacheKey);
  if (hit) {
    const response = new Response(hit.body, hit);
    response.headers.set("X-Jarvis-Cache", "HIT");
    return response;
  }

  const payload = repo(env) && env.MY_GITHUB_TOKEN ? await liveState(env) : demoState(env);
  const response = json(payload);
  response.headers.set("X-Jarvis-Cache", "MISS");
  response.headers.set("Cache-Control", `public, max-age=${CACHE_TTL_SECONDS}`);
  if (ctx && ctx.waitUntil) ctx.waitUntil(cache.put(cacheKey, response.clone()));
  return response;
}

async function liveState(env) {
  const limit = Number(env.STATE_LIMIT || 14);
  const [tasks, memory, deploys] = await Promise.all([
    supabaseRows(
      env,
      "tasks_log",
      "select=id,created_at,issue_number,issue_title,status,summary,pr_url,pr_number,llm_provider,llm_model&order=id.desc",
      limit
    ),
    supabaseRows(
      env,
      "system_memory",
      "select=kind,key,content,importance,updated_at&order=importance.desc,updated_at.desc",
      12
    ),
    supabaseRows(env, "deployments", "select=target_name,target_type,status,url,created_at&order=id.desc", 10),
  ]);

  let issues = [];
  try {
    const labels = String(env.TASK_LABELS || "task,devops").split(",").map((x) => x.trim()).filter(Boolean).join(",");
    const path = `/repos/${repo(env)}/issues?state=open&sort=updated&direction=desc&per_page=12${labels ? `&labels=${encodeURIComponent(labels)}` : ""}`;
    const { status, body } = await githubJson(env, path);
    if (status === 200 && Array.isArray(body)) {
      issues = body
        .filter((item) => !item.pull_request)
        .map((item) => ({
          number: item.number,
          title: item.title || "",
          labels: (item.labels || []).map((label) => (typeof label === "string" ? label : label.name)),
          url: item.html_url,
          updated_at: item.updated_at,
        }));
    }
  } catch {
    issues = [];
  }

  const completed = tasks.filter((task) => String(task.status) === "completed").length;
  return {
    mode: "live",
    repo: repo(env),
    generated_at: new Date().toISOString(),
    vitals: vitals(env, true),
    stats: {
      tasks: tasks.length,
      success: tasks.length ? Math.round((completed / tasks.length) * 100) : null,
      prs: tasks.filter((task) => task.pr_url).length,
      deploys: deploys.length,
    },
    tasks: tasks.map((task) => ({
      id: task.id,
      issue: task.issue_number,
      title: task.issue_title || task.summary || "",
      status: task.status,
      provider: task.llm_provider,
      model: task.llm_model,
      pr_url: task.pr_url || "",
      created_at: task.created_at,
    })),
    memory,
    deploys: deploys.map((item) => ({
      target: item.target_name,
      type: item.target_type,
      status: item.status,
      url: item.url || "",
      created_at: item.created_at,
    })),
    issues,
    runs: tasks.slice(0, 8).map((task) => ({
      kind: String(task.status).includes("fail") ? "fail" : String(task.status).includes("run") ? "run" : "ok",
      title: `#${task.issue_number || "—"} ${task.issue_title || task.summary || ""}`,
      meta: [task.llm_model, task.status].filter(Boolean).join(" · "),
      at: task.created_at,
    })),
  };
}

function vitals(env, live) {
  const has = (name) => Boolean(env[name]);
  const state = (ok, optional) => (ok ? "ok" : optional ? "warn" : "off");
  return [
    { name: "GitHub", sub: "MY_GITHUB_TOKEN", state: state(has("MY_GITHUB_TOKEN")), value: has("MY_GITHUB_TOKEN") ? 100 : 6, note: repo(env) },
    { name: "Groq", sub: String(env.GROQ_MODELS || "llama-3.3-70b-versatile").split(",")[0], state: state(has("GROQ_API_KEY")), value: has("GROQ_API_KEY") ? 92 : 6, note: "fast cortex" },
    { name: "Gemini", sub: String(env.GEMINI_MODELS || "gemini-2.0-flash").split(",")[0], state: state(has("GEMINI_API_KEY")), value: has("GEMINI_API_KEY") ? 88 : 6, note: "deep cortex" },
    { name: "Supabase", sub: "tasks_log · system_memory", state: state(has("SUPABASE_URL") && has("SUPABASE_SERVICE_ROLE_KEY")), value: has("SUPABASE_URL") ? 94 : 6, note: "long-term memory" },
    { name: "Cloudflare", sub: "workers · pages", state: state(has("CLOUDFLARE_API_TOKEN"), true), value: has("CLOUDFLARE_API_TOKEN") ? 76 : 20, note: "edge link" },
    { name: "MCP", sub: "context7 · github · supabase", state: "ok", value: 62, note: "model context protocol" },
    { name: "Dashboard", sub: live ? "live" : "demo", state: state(live, true), value: live ? 100 : 45, note: repo(env) },
  ];
}

function demoState(env) {
  const now = Date.now();
  const ago = (minutes) => new Date(now - minutes * 60000).toISOString();
  return {
    mode: "demo",
    repo: repo(env) || "owner/Abu-Ali (demo)",
    generated_at: new Date().toISOString(),
    vitals: vitals(env, false),
    stats: { tasks: 48, success: 92, prs: 17, deploys: 5 },
    tasks: [
      { id: 48, issue: 42, title: "إضافة endpoint للفحص الصحي", status: "completed", provider: "gemini", model: "gemini-2.0-flash", pr_url: "https://github.com/owner/repo/pull/42", created_at: ago(6) },
      { id: 47, issue: 41, title: "تحديث واجهة لوحة التحكم", status: "running", provider: "groq", model: "llama-3.3-70b-versatile", pr_url: "", created_at: ago(18) },
      { id: 46, issue: 40, title: "نشر Worker على Cloudflare", status: "completed", provider: "groq", model: "llama-3.3-70b-versatile", pr_url: "https://github.com/owner/repo/pull/40", created_at: ago(75) },
      { id: 45, issue: 39, title: "إصلاح خطأ في تسجيل الدخول", status: "failed", provider: "gemini", model: "gemini-1.5-pro", pr_url: "", created_at: ago(160) },
    ],
    memory: [
      { kind: "architecture", key: "health-endpoint", content: "كل فحوصات الصحة في health/check.py", importance: 7, updated_at: ago(6) },
      { kind: "preference", key: "language-arabic", content: "المالك يفضّل التقارير بالعربية", importance: 9, updated_at: ago(60) },
      { kind: "decision", key: "edge-workers", content: "الواجهة تُنشر كـ Cloudflare Worker باسم abu-ali-jarvis", importance: 8, updated_at: ago(180) },
    ],
    deploys: [
      { target: "abu-ali-jarvis", type: "worker", status: "deployed", url: "https://abu-ali-jarvis.workers.dev", created_at: ago(30) },
    ],
    issues: [
      { number: 42, title: "أضف endpoint للفحص الصحي", labels: ["task"], url: "https://github.com/owner/repo/issues/42", updated_at: ago(9) },
      { number: 41, title: "حدّث واجهة لوحة التحكم", labels: ["task", "devops"], url: "https://github.com/owner/repo/issues/41", updated_at: ago(22) },
    ],
    runs: [
      { kind: "ok", title: "مهمة #42 أُنجزت — PR مفتوح", meta: "gemini-2.0-flash · completed", at: ago(6) },
      { kind: "run", title: "مهمة #41 قيد التنفيذ…", meta: "llama-3.3-70b · running", at: ago(18) },
      { kind: "info", title: "الواجهة تعمل بلا مفاتيح — أضف الأسرار لتنشيطها", meta: "demo", at: ago(1) },
    ],
  };
}

/* --------------------------------------------------------------------------
 *  actions
 * ------------------------------------------------------------------------ */

async function dispatchCommand(env, body) {
  if (!repo(env) || !env.MY_GITHUB_TOKEN) {
    return { ok: false, demo: true, message: "وضع العرض: أضف MY_GITHUB_TOKEN و GITHUB_REPOSITORY لتنفيذ حقيقي." };
  }
  const instruction = String(body.instruction || "").trim();
  if (!instruction) return { ok: false, message: "الأمر فارغ" };

  const inputs = { instruction, dry_run: "false" };
  if (body.issue) inputs.issue_number = String(body.issue);

  const { status, body: response } = await githubJson(
    env,
    `/repos/${repo(env)}/actions/workflows/${workflowFile(env)}/dispatches`,
    { method: "POST", body: JSON.stringify({ ref: defaultBranch(env), inputs }) }
  );

  if (status === 204 || status === 201) {
    if (env.SUPABASE_URL && env.SUPABASE_SERVICE_ROLE_KEY) {
      try {
        await fetch(`${String(env.SUPABASE_URL).replace(/\/$/, "")}/rest/v1/interactions`, {
          method: "POST",
          headers: { ...supabaseHeaders(env), "Content-Type": "application/json", Prefer: "return=minimal" },
          body: JSON.stringify({
            repo: repo(env),
            author: "jarvis-dashboard",
            role: "user",
            body: instruction.slice(0, 8000),
            metadata: { source: "jarvis-worker" },
          }),
        });
      } catch {
        /* logging the interaction is a bonus, never a blocker */
      }
    }
    return {
      ok: true,
      message: "أُرسل الأمر إلى GitHub Actions — سيبدأ التنفيذ خلال ثوانٍ.",
      run_url: `https://github.com/${repo(env)}/actions/workflows/${workflowFile(env)}`,
    };
  }

  const message = response && response.message ? response.message : `HTTP ${status}`;
  const hint = status === 404
    ? " (تأكد أن ملف الـ Workflow موجود على الفرع الافتراضي وأن التوكن يملك صلاحية workflow)"
    : "";
  return { ok: false, message: `فشل الإرسال (${status}): ${message}${hint}` };
}

function stripWorkflowHeader(text) {
  const lines = String(text).split("\n");
  if (lines.length && lines[0].startsWith("#")) {
    for (let index = 1; index < lines.length; index++) {
      if (lines[index].startsWith("# ====")) {
        return lines.slice(index + 1).join("\n").replace(/^\n+/, "");
      }
    }
  }
  return String(text);
}

async function installWorkflow(env) {
  if (!repo(env) || !env.MY_GITHUB_TOKEN) {
    return { ok: false, message: "وضع العرض: التثبيت يحتاج توكن GitHub حقيقي (صلاحية workflow)." };
  }
  const content = btoa(unescape(encodeURIComponent(stripWorkflowHeader(WORKFLOW_YML))));
  const current = await githubJson(env, `/repos/${repo(env)}/contents/${WORKFLOW_TARGET}?ref=${defaultBranch(env)}`);
  const sha = current.status === 200 && current.body ? current.body.sha : null;

  const payload = {
    message: "ci: install the Abu Ali orchestrator workflow [skip ci]",
    content,
    branch: defaultBranch(env),
  };
  if (sha) payload.sha = sha;

  const { status, body } = await githubJson(env, `/repos/${repo(env)}/contents/${WORKFLOW_TARGET}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });

  if (status === 200 || status === 201) {
    const url = (body && body.content && body.content.html_url) || `https://github.com/${repo(env)}/blob/${defaultBranch(env)}/${WORKFLOW_TARGET}`;
    return { ok: true, message: "تم تثبيت ملف الـ Workflow ✔", url };
  }
  const message = body && body.message ? body.message : `HTTP ${status}`;
  return { ok: false, message: `فشل التثبيت (${status}): ${message}${status === 403 ? " — التوكن يحتاج صلاحية `workflow`" : ""}` };
}
