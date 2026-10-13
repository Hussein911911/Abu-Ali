/**
 * =============================================================================
 *  Abu Ali — offline test for the J.A.R.V.I.S. Cloudflare Worker
 * =============================================================================
 *  Cloudflare bundles `import html from "./index.html"` as text modules; plain
 *  Node cannot, so this harness copies the *unmodified* logic into a temp file
 *  and swaps only those two import statements for file reads. Everything else
 *  — routing, authorisation, GitHub/Supabase calls, state building — runs for
 *  real against a stubbed global fetch.
 *
 *      node tests/test_worker.mjs
 * =============================================================================
 */

import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import assert from "node:assert/strict";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const WORKER = join(ROOT, "jarvis", "worker.js");
const INDEX = join(ROOT, "jarvis", "index.html");
const WORKFLOW = join(ROOT, "deploy", "abu_ali_agent.yml");

const results = [];
function check(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => {
      results.push({ name, ok: true });
      console.log(`  ✔ ${name}`);
    })
    .catch((error) => {
      results.push({ name, ok: false, message: error.message });
      console.log(`  ✘ ${name}\n      ${error.message}`);
    });
}

/* -------------------------------------------------------------------------- */
/*  build a Node-importable copy of worker.js                                  */
/* -------------------------------------------------------------------------- */
const dir = mkdtempSync(join(tmpdir(), "abu-worker-"));
const target = join(dir, "worker.mjs");

let source = readFileSync(WORKER, "utf8");
source = source
  .replace(/^import INDEX_HTML from "\.\/index\.html";$/m, `import { readFileSync as __rf } from "node:fs";\nconst INDEX_HTML = __rf(${JSON.stringify(INDEX)}, "utf8");`)
  .replace(/^import WORKFLOW_YML from "\.\.\/deploy\/abu_ali_agent\.yml";$/m, `const WORKFLOW_YML = __rf(${JSON.stringify(WORKFLOW)}, "utf8");`);

assert.ok(source.includes("const INDEX_HTML"), "index.html text import was not rewritten");
assert.ok(source.includes("const WORKFLOW_YML"), "workflow text import was not rewritten");
writeFileSync(target, source, "utf8");

/* -------------------------------------------------------------------------- */
/*  fetch stub: records calls, serves canned GitHub/Supabase responses        */
/* -------------------------------------------------------------------------- */
const calls = [];
function makeFetchStub(state) {
  return async function fetchStub(url, init = {}) {
    const target = String(url);
    calls.push({ url: target, method: (init.method || "GET").toUpperCase(), body: init.body });

    if (target.includes("api.github.com") && target.includes("/issues?")) {
      return new Response(JSON.stringify([
        { number: 42, title: "أضف فحص صحي", html_url: "https://github.com/o/r/issues/42", updated_at: new Date().toISOString(), labels: [{ name: "task" }] },
        { number: 43, title: "PR not an issue", pull_request: {}, labels: [] },
      ]), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (target.includes("/actions/workflows/") && target.includes("/dispatches")) {
      state.dispatchStatus = state.dispatchStatus ?? 204;
      // 204 must be body-less, otherwise the Response constructor throws
      return state.dispatchStatus === 204
        ? new Response(null, { status: 204 })
        : new Response(JSON.stringify({ message: "Workflow does not exist" }), { status: state.dispatchStatus });
    }
    if (target.includes("/contents/.github/workflows/")) {
      state.installStatus = state.installStatus ?? 201;
      return new Response(JSON.stringify(state.installStatus === 201 ? { content: { html_url: "https://github.com/o/r/blob/main/.github/workflows/abu_ali_agent.yml" } } : { message: "Resource not accessible by integration" }), { status: state.installStatus });
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
      return new Response(JSON.stringify([{ target_name: "abu-ali-jarvis", target_type: "worker", status: "deployed", url: "https://x.workers.dev", created_at: new Date().toISOString() }]), { status: 200 });
    }
    if (target.includes("/rest/v1/interactions")) {
      return new Response(null, { status: 201 });
    }
    return new Response(JSON.stringify({ message: "unmapped" }), { status: 404 });
  };
}

/* -------------------------------------------------------------------------- */
/*  tests                                                                     */
/* -------------------------------------------------------------------------- */
const worker = (await import(pathToFileURL(target).href)).default;
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
const state = {};
globalThis.fetch = makeFetchStub(state);
globalThis.caches = { default: { match: async () => undefined, put: async () => {} } };
const ctx = { waitUntil: () => {} };

console.log("\n=== Abu Ali J.A.R.V.I.S. Worker — offline tests ===\n");

await check("GET / serves the HUD with the reactor markup", async () => {
  const res = await worker.fetch(new Request("https://jarvis.test/"), liveEnv, ctx);
  const html = await res.text();
  assert.equal(res.status, 200);
  assert.ok(html.includes("J.A.R.V.I.S."), "brand missing");
  assert.ok(html.includes('id="reactor"'), "reactor missing");
  assert.ok(html.includes("/api/state"), "api wiring missing");
  assert.ok(!html.includes("localhost"), "must never reference localhost");
});

await check("demo mode when no credentials are wired", async () => {
  const res = await worker.fetch(new Request("https://jarvis.test/api/state"), demoEnv, ctx);
  const body = await res.json();
  assert.equal(body.mode, "demo");
  assert.ok(body.tasks.length > 0, "demo tasks missing");
  assert.ok(body.vitals.length >= 6, "vitals missing");
});

await check("live mode aggregates GitHub + Supabase", async () => {
  const res = await worker.fetch(new Request("https://jarvis.test/api/state"), liveEnv, ctx);
  const body = await res.json();
  assert.equal(body.mode, "live");
  assert.equal(body.repo, "o/r");
  assert.equal(body.tasks.length, 2);
  assert.equal(body.stats.success, 50, `expected 50% success, got ${body.stats.success}`);
  assert.equal(body.issues.length, 1, "pull requests must be filtered out of issues");
  assert.equal(body.deploys[0].target, "abu-ali-jarvis");
  assert.ok(body.runs.some((run) => run.kind === "fail"), "failed task should surface as a fail event");
});

await check("POST /api/command dispatches the workflow", async () => {
  const res = await worker.fetch(
    new Request("https://jarvis.test/api/command", {
      method: "POST",
      body: JSON.stringify({ instruction: "/abu افحص المستودع", issue: 42 }),
      headers: { "Content-Type": "application/json" },
    }),
    liveEnv,
    ctx
  );
  const body = await res.json();
  assert.equal(body.ok, true, JSON.stringify(body));
  const dispatched = calls.find((call) => call.url.includes("/dispatches"));
  assert.ok(dispatched, "no dispatch call was made");
  const payload = JSON.parse(dispatched.body);
  assert.equal(payload.ref, "main");
  assert.equal(payload.inputs.instruction, "/abu افحص المستودع");
  assert.equal(payload.inputs.issue_number, "42");
});

await check("POST /api/command reports a 404 clearly (workflow not installed)", async () => {
  state.dispatchStatus = 404;
  const res = await worker.fetch(
    new Request("https://jarvis.test/api/command", { method: "POST", body: JSON.stringify({ instruction: "x" }) }),
    liveEnv,
    ctx
  );
  const body = await res.json();
  assert.equal(body.ok, false);
  assert.ok(body.message.includes("404"), body.message);
  assert.ok(body.message.includes("workflow"), "should hint about the workflow scope");
  state.dispatchStatus = 204;
});

await check("POST /api/install-workflow writes the CI file", async () => {
  const res = await worker.fetch(new Request("https://jarvis.test/api/install-workflow", { method: "POST", body: "{}" }), liveEnv, ctx);
  const body = await res.json();
  assert.equal(body.ok, true, JSON.stringify(body));
  const put = calls.filter((call) => call.method === "PUT").pop();
  const payload = JSON.parse(put.body);
  const decoded = Buffer.from(payload.content, "base64").toString("utf8");
  assert.ok(decoded.startsWith("name: Abu Ali"), "the installed file must start with the workflow name");
  assert.ok(!decoded.startsWith("# ===="), "the canonical-copy header must be stripped");
  assert.equal(payload.branch, "main");
});

await check("POST /api/install-workflow explains a 403 (missing workflow scope)", async () => {
  state.installStatus = 403;
  const res = await worker.fetch(new Request("https://jarvis.test/api/install-workflow", { method: "POST", body: "{}" }), liveEnv, ctx);
  const body = await res.json();
  assert.equal(body.ok, false);
  assert.ok(body.message.includes("workflow"), body.message);
  state.installStatus = 201;
});

await check("JARVIS_ACCESS_KEY gates /api/*", async () => {
  const gated = { ...liveEnv, JARVIS_ACCESS_KEY: "s3cret" };
  const denied = await worker.fetch(new Request("https://jarvis.test/api/state"), gated, ctx);
  assert.equal(denied.status, 401);
  const allowed = await worker.fetch(new Request("https://jarvis.test/api/state?key=s3cret"), gated, ctx);
  assert.equal(allowed.status, 200);
  const header = await worker.fetch(new Request("https://jarvis.test/api/state", { headers: { "x-jarvis-key": "s3cret" } }), gated, ctx);
  assert.equal(header.status, 200);
});

await check("unknown routes are refused", async () => {
  const res = await worker.fetch(new Request("https://jarvis.test/api/nope"), liveEnv, ctx);
  assert.equal(res.status, 404);
  const html = await worker.fetch(new Request("https://jarvis.test/whatever"), liveEnv, ctx);
  assert.equal(html.status, 404);
});

const failed = results.filter((result) => !result.ok);
console.log(`\n=== ${results.length - failed.length}/${results.length} checks passed${failed.length ? "" : " — ALL GREEN ✔"} ===\n`);
process.exit(failed.length ? 1 : 0);
