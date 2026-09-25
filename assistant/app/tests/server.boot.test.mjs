// -----------------------------------------------------------
//  [*] Tests — the entry point boots
//
//  server.js itself, imported under PORT=0: the app listens,
//  /health answers, all three route families are mounted
//  where Caddy expects them, and the error middleware is
//  registered LAST — a thrown HttpError from a mounted route
//  reaches the wire as the envelope the mobile taxonomy
//  parses. Nothing here needs Django or a model: every
//  request below is refused or answered before either is
//  asked (a guest list, a bodiless chat turn, the static
//  tools contract).
//
//    docker exec knfapp-assistant npm test
// -----------------------------------------------------------

import assert from "node:assert/strict";
import { after, test } from "node:test";


// Set BEFORE the import: config.js reads the env once, and
// server.js listens at import time. PORT=0 takes an ephemeral
// port; the gateway pair makes the chat route "configured"
// so it proceeds to validate the body instead of answering
// 503 NOT_CONFIGURED; the Django URL points nowhere on
// purpose — no request below may reach it
process.env.PORT = "0";
process.env.DJANGO_URL = "http://127.0.0.1:9/";
process.env.ASSISTANT_INTERNAL_SECRET = "boot-test-secret";
process.env.AI_GATEWAY_URL = "http://127.0.0.1:9/v1";
process.env.AI_GATEWAY_KEY = "boot-test-key";

const { server } = await import("../server.js");
if (!server.listening) {
  await new Promise((resolve) => server.once("listening", resolve));
}
const BASE = `http://127.0.0.1:${server.address().port}`;

after(() => new Promise((resolve) => server.close(resolve)));


test("the server listens on the configured port and /health reports the model AND secret configuration", async () => {
  assert.ok(server.address().port > 0, "an ephemeral port was taken");
  const response = await fetch(`${BASE}/health`);
  assert.equal(response.status, 200);
  // `internal` is new: a deploy missing ASSISTANT_INTERNAL_SECRET
  // answered a plain "ok" while Django refused every relay
  assert.deepEqual(await response.json(), { status: "ok", model: "configured", internal: "configured" });
});


test("/api/assistant/tools is mounted and serves the contract envelope, cacheable", async () => {
  const response = await fetch(`${BASE}/api/assistant/tools`);
  assert.equal(response.status, 200);
  assert.match(response.headers.get("cache-control") || "", /max-age=3600/);
  const body = await response.json();
  assert.deepEqual(body.tools.map((tool) => tool.name), ["lookupSchedule", "searchNews", "searchHandbook"]);
});


test("/api/assistant/threads is mounted: a guest list is the 401 envelope through the real error middleware", async () => {
  const response = await fetch(`${BASE}/api/assistant/threads`);
  assert.equal(response.status, 401);
  const body = await response.json();
  assert.equal(body.error.code, "SIGN_IN_REQUIRED");
  assert.equal(body.message, body.error.message);
});


test("/api/assistant/chat is mounted and parses JSON: a bodiless guest turn is the 400 envelope", async () => {
  const response = await fetch(`${BASE}/api/assistant/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  assert.equal(response.status, 400);
  assert.equal((await response.json()).error.code, "INVALID_MESSAGES");
});


test("a malformed JSON body is the 400 INVALID_BODY envelope, not a 500 — the parser's refusal stays the client's fault", async () => {
  const response = await fetch(`${BASE}/api/assistant/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{ not json",
  });
  assert.equal(response.status, 400);
  const body = await response.json();
  assert.equal(body.error.code, "INVALID_BODY");
  assert.equal(body.message, body.error.message);
});
