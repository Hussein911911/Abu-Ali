/**
 * =============================================================================
 *  Abu Ali — offline test for the Cloudflare PAGES Function
 *  (functions/api/[[path]].js)
 * =============================================================================
 *  The Pages Function has no build-specific imports, so Node can import it
 *  directly and exercise the real code against a stubbed global fetch.
 *
 *      node tests/test_pages_function.mjs
 * =============================================================================
 */

import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const FUNCTION_PATH = join(ROOT, "functions", "api", "[[path]].js");
const WORKFLOW_LOCAL = join(ROOT, "deploy", "abu_ali_agent.yml");

import { readFileSync } from "node:fs";
const WORKFLOW_TEXT = readFileSync(WORKFLOW_LOCAL, "utf8");

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push({ name, ok: true });
    console.log(`  ✔ ${name}`);
  } catch (error) {
    results.push({ name, ok: false, message: error.message });
    console.log(`  ✘ ${name}\n      ${error.message}`);
  }
}

/* --------------------------- fetch stub ---------------------------------- */
const calls = [];
const state = { dispatchStatus: 204, installStatus: 201 };

globalThis.fetch = async function fetchStub(url, init = {}) {
  const target = String(url);
  calls.push({ url: target, method: (init.method || "GET").toUpperCase(), body: init.body });

  if (target.includes("/contents/deploy/abu_ali_agent.yml")) {
    return new Response(JSON.stringify({ content: Buffer.from(WORKFLOW_TEXT, "utf8").toString("base64"), sha: "abc123" }), { status: 200 });
  }
  if (target.includes("/contents/.github/workflows/")) {
    // the function first GETs the file (to reuse its sha) and then PUTs the update
    if ((init.method || "GET").toUpperCase() === "GET") {
      return new Response(JSON.stringify({ sha: "existing-sha", content: { html_url: "https://github.com/o/r/blob/main/.github/workflows/abu_ali_agent.yml" } }), { status: 200 });
    }
    if (state.installStatus === 201) {
      return new Response(JSON.stringify({ content: { html_url: "https://github.com/o/r/blob/main/.github/workflows/abu_ali_agent.yml" } }), { status: 201 });
    }
    return new Response(JSON.stringify({ message: "Resource not accessible by integration" }), { status: state.installStatus });
  }
  if (target.includes("/actions/workflows/") && target.includes("/dispatches")) {
    return state.dispatchStatus === 204
      ? new Response(null, { status: 204 })
      : new Response(JSON.stringify({ message: "Workflow does not exist" }), { status: state.dispatchStatus });
  }
  if (target.includes("/issues?")) {
    return new Response(JSON.stringify([
      { number: 42, title: "أضف فحص صحي", html_url: "https://github.com/o/r/issues/42", updated_at: new Date().toISOString(), labels: [{ name: "task" }] },
      { number: 43, title: "PR", pull_request: {}, labels: [] },
    ]), { status: 200 });
  }
  if (target.includes("/rest/v1/tasks_log")) {
    return new Response(JSON.stringify([
      { id: 2, issue_number: 42, issue_title: "أضف فحص صحي", status: "completed", pr_url: "https://github.com/o/r/pull/42", llm_provider: "gemini", llm_model: "gemini-2.0-flash", created_at: new Date().toISOString() },
      { id: 1, issue_number: 41, issue_title: "حدّث الواجهة", status: "failed", pr_url: "", llm_provider: "groq", llm_model: "llama-3.3-70b", created_at: new Date().toISOString() },
    ]), { status: 200 });
  }
  if (target.includes("/rest/v1/system_memory")) {
    return new Response(JSON.stringify([{ kind: "fact", key: "k", content: "supabase works", importance: 7, updated_at: new Date().toISOString() }]), { status: 200 });
  }
  if (target.includes("/rest/v1/deployments")) {
    return new Response(JSON.stringify([{ target_name: "abu-ali", target_type: "pages", status: "deployed", url: "https://abu-ali.pages.dev", created_at: new Date().toISOString() }]), { status: 200 });
  }
  if (target.includes("/rest/v1/interactions")) {
    return new Response(null, { status: 201 });
  }
  return new Response(JSON.stringify({ message: "unmapped" }), { status: 404 });
};

globalThis.caches = { default: { match: async () => undefined, put: async () => {} } };

/* --------------------------- import the function ------------------------- */
const mod = await import(pathToFileURL(FUNCTION_PATH).href);
assert.equal(typeof mod.onRequest, "function", "the module must export onRequest");

const liveEnv = {
  MY_GITHUB_TOKEN: "fake-token",
  SUPABASE_URL: "https://fake.supabase.co",
  SUPABASE_SERVICE_ROLE_KEY: "fake-service-key",
  GITHUB_REPOSITORY: "o/r",
  ABU_WORKFLOW_FILE: "abu_ali_agent.yml",
  ABU_DEFAULT_BRANCH: "main",
  TASK_LABELS: "task,devops",
};
const demoEnv = { GITHUB_REPOSITORY: "" };
const ctx = { request: null, env: null, waitUntil: () => {} };

function call(path, env, { method = "GET", body = null, headers = {} } = {}) {
  const request = new Request(`https://abu-ali.pages.dev${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  return mod.onRequest({ ...ctx, request, env });
}

console.log("\n=== Abu Ali J.A.R.V.I.S. Pages Function — offline tests ===\n");

await check("GET /api/health reports the platform and mode", async () => {
  const demo = await (await call("/api/health", demoEnv)).json();
  assert.equal(demo.ok, true);
  assert.equal(demo.mode, "demo");
  assert.equal(demo.platform, "cloudflare-pages");
  const live = await (await call("/api/health", liveEnv)).json();
  assert.equal(live.mode, "live");
  assert.equal(live.repo, "o/r");
});

await check("GET /api/state (demo) returns a full HUD payload", async () => {
  const body = await (await call("/api/state", demoEnv)).json();
  assert.equal(body.mode, "demo");
  assert.ok(body.tasks.length > 0 && body.memory.length > 0 && body.vitals.length >= 6);
});

await check("GET /api/state (live) aggregates GitHub + Supabase", async () => {
  const body = await (await call("/api/state", liveEnv)).json();
  assert.equal(body.mode, "live");
  assert.equal(body.stats.success, 50);
  assert.equal(body.issues.length, 1, "pull requests must be filtered out");
  assert.equal(body.deploys[0].target, "abu-ali");
  assert.equal(body.platform, "cloudflare-pages");
});

await check("POST /api/command dispatches the workflow with the right inputs", async () => {
  const res = await call("/api/command", liveEnv, { method: "POST", body: { instruction: "/abu افحص المستودع", issue: 42 }, headers: { "Content-Type": "application/json" } });
  const body = await res.json();
  assert.equal(body.ok, true, JSON.stringify(body));
  const dispatched = calls.filter((c) => c.url.includes("/dispatches")).pop();
  const payload = JSON.parse(dispatched.body);
  assert.equal(payload.ref, "main");
  assert.equal(payload.inputs.issue_number, "42");
  assert.ok(payload.inputs.instruction.includes("افحص المستودع"));
});

await check("POST /api/command refuses an empty instruction", async () => {
  const res = await call("/api/command", liveEnv, { method: "POST", body: { instruction: "   " }, headers: { "Content-Type": "application/json" } });
  const body = await res.json();
  assert.equal(body.ok, false);
  assert.ok(body.message.includes("فارغ"));
});

await check("POST /api/command explains a 404 (workflow not installed yet)", async () => {
  state.dispatchStatus = 404;
  const body = await (await call("/api/command", liveEnv, { method: "POST", body: { instruction: "x" }, headers: { "Content-Type": "application/json" } })).json();
  assert.equal(body.ok, false);
  assert.ok(body.message.includes("404") && body.message.includes("workflow"), body.message);
  state.dispatchStatus = 204;
});

await check("POST /api/install-workflow pulls deploy/ and writes .github/workflows/", async () => {
  const body = await (await call("/api/install-workflow", liveEnv, { method: "POST", body: {}, headers: { "Content-Type": "application/json" } })).json();
  assert.equal(body.ok, true, JSON.stringify(body));
  const put = calls.filter((c) => c.method === "PUT").pop();
  const payload = JSON.parse(put.body);
  const decoded = Buffer.from(payload.content, "base64").toString("utf8");
  assert.ok(decoded.startsWith("name: Abu Ali"), "installed file must start with the workflow name");
  assert.ok(!decoded.startsWith("# ===="), "canonical-copy header must be stripped");
  assert.equal(payload.sha, "existing-sha", "existing file sha must be reused for the update");
});

await check("POST /api/install-workflow explains a 403 (missing workflow scope)", async () => {
  state.installStatus = 403;
  const body = await (await call("/api/install-workflow", liveEnv, { method: "POST", body: {}, headers: { "Content-Type": "application/json" } })).json();
  assert.equal(body.ok, false);
  assert.ok(body.message.includes("workflow"), body.message);
  state.installStatus = 201;
});

await check("JARVIS_ACCESS_KEY gates /api/*", async () => {
  const gated = { ...liveEnv, JARVIS_ACCESS_KEY: "s3cret" };
  const denied = await call("/api/state", gated);
  assert.equal(denied.status, 401);
  assert.equal((await call("/api/state?key=s3cret", gated)).status, 200);
  assert.equal((await call("/api/state", gated, { headers: { "x-jarvis-key": "s3cret" } })).status, 200);
});

await check("unknown /api routes 404 and OPTIONS is handled", async () => {
  assert.equal((await call("/api/nope", liveEnv)).status, 404);
  const options = await call("/api/state", liveEnv, { method: "OPTIONS" });
  assert.equal(options.status, 204);
});

const failed = results.filter((r) => !r.ok);
console.log(`\n=== ${results.length - failed.length}/${results.length} checks passed${failed.length ? "" : " — ALL GREEN ✔"} ===\n`);
process.exit(failed.length ? 1 : 0);
