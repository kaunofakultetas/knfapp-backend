// -----------------------------------------------------------
//  [*] Tests — the thread routes, whole
//
//  The REAL thread router top to bottom — real Express app,
//  real identity resolution, real relay fetches wearing the
//  real secret header — against the scripted stub Django.
//  Seven endpoints, one discipline: resolve who is asking,
//  refuse what must never reach the internal wire (a
//  non-uuid path segment, a malformed or oversized id list,
//  a guest on a signed-in-only route, a nonsense feedback
//  body), relay everything else with the identity attached
//  and hand Django's answer back unchanged — its status
//  included, so a Django 404 IS the phone's 404.
//
//    docker exec knfapp-assistant npm test
// -----------------------------------------------------------

import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";

import { startStubDjango, STUB_SECRET } from "./stubDjango.mjs";


// The stub must exist before any app import — config.js
// reads these once, at first import
const stub = await startStubDjango();
process.env.DJANGO_URL = stub.url;
process.env.ASSISTANT_INTERNAL_SECRET = STUB_SECRET;

const { default: express } = await import("express");
const { default: threadRoutes } = await import("../routes/threads.js");
const { errorMiddleware } = await import("../middleware/errors.js");

// The same lines server.js gives the app
const app = express();
app.set("trust proxy", true);
app.use(express.json({ limit: "2mb" }));
app.use("/api/assistant/threads", threadRoutes);
app.use(errorMiddleware);
const server = app.listen(0, "127.0.0.1");
await new Promise((resolve) => server.once("listening", resolve));
const BASE = `http://127.0.0.1:${server.address().port}/api/assistant/threads`;

after(async () => {
  server.close();
  await stub.close();
});


const UUID = "0f9b7d1e-4c6a-4b1e-9a2f-3d5c7e9b1a2c";
const OTHER = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d";

const call = (path, { method = "GET", body, token, headers = {} } = {}) => fetch(`${BASE}${path}`, {
  method,
  headers: {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...headers,
  },
  body: body === undefined ? undefined : JSON.stringify(body),
});

const relayed = (path, method = "POST") => stub.requests.find((r) => r.path === path && r.method === method);
const internalCalls = () => stub.requests.filter((r) => r.path.startsWith("/internal/"));


beforeEach(() => {
  stub.requests.length = 0;
  stub.script.threads.clear();
  stub.script.transcripts.clear();
  stub.script.nextThreadId = "thread-minted";
  stub.script.users.set("tok-ona", { id: "u-ona" });
});


// ---------------------------------------------------------
// POST / — create
// ---------------------------------------------------------

test("a guest creates a thread with no user and the header's language", async () => {
  const response = await call("/", { method: "POST", body: {}, headers: { "Accept-Language": "en-GB,en;q=0.9" } });
  assert.equal(response.status, 201);
  const created = await response.json();
  assert.equal(created.id, "thread-minted");
  assert.equal(created.user_id, null);

  const relay = relayed("/internal/assistant/threads");
  assert.equal(relay.body.user_id, null);
  assert.equal(relay.body.language, "en");
  assert.equal(relay.headers["x-internal-secret"], STUB_SECRET, "the relay wears the secret");
});


test("a signed-in create attaches the resolved user, and the body's language beats the header", async () => {
  const response = await call("/", { method: "POST", body: { language: "lt" }, token: "tok-ona",
                                     headers: { "Accept-Language": "en" } });
  assert.equal(response.status, 201);
  const relay = relayed("/internal/assistant/threads");
  assert.equal(relay.body.user_id, "u-ona");
  assert.equal(relay.body.language, "lt");
});


test("an invalid bearer on create is the 401 envelope, and nothing reaches the internal wire", async () => {
  const response = await call("/", { method: "POST", body: {}, token: "tok-nobody" });
  assert.equal(response.status, 401);
  assert.equal((await response.json()).error.code, "SESSION_INVALID");
  assert.equal(internalCalls().length, 0);
});


// ---------------------------------------------------------
// GET / — the signed-in list
// ---------------------------------------------------------

test("a guest listing is 401 SIGN_IN_REQUIRED before any relay; a user gets the list keyed on their id", async () => {
  const guest = await call("/");
  assert.equal(guest.status, 401);
  assert.equal((await guest.json()).error.code, "SIGN_IN_REQUIRED");
  assert.equal(internalCalls().length, 0);

  stub.script.threads.set(UUID, { user_id: "u-ona" });
  stub.script.threads.set(OTHER, { user_id: "u-somebody-else" });
  const mine = await call("/", { token: "tok-ona" });
  assert.equal(mine.status, 200);
  assert.deepEqual((await mine.json()).threads.map((t) => t.id), [UUID]);
  assert.equal(relayed("/internal/assistant/threads/list", "GET").query.user_id, "u-ona");
});


// ---------------------------------------------------------
// POST /lookup and /claim — requireIdList
// ---------------------------------------------------------

test("lookup refuses a missing, non-array or oversized id list with 400 INVALID_IDS, relaying nothing", async () => {
  for (const body of [{}, { ids: "abc" }, { ids: Array.from({ length: 101 }, (_, i) => `id-${i}`) }]) {
    const response = await call("/lookup", { method: "POST", body });
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error.code, "INVALID_IDS");
  }
  assert.equal(internalCalls().length, 0);
});


test("lookup relays the ids as strings with the caller's identity (null for a guest)", async () => {
  stub.script.threads.set(UUID, { user_id: null });
  const response = await call("/lookup", { method: "POST", body: { ids: [UUID, 42] } });
  assert.equal(response.status, 200);
  assert.deepEqual((await response.json()).threads.map((t) => t.id), [UUID]);
  const relay = relayed("/internal/assistant/threads/lookup");
  assert.deepEqual(relay.body, { ids: [UUID, "42"], user_id: null });
});


test("claim needs a session, validates the list, then adopts the ownerless threads", async () => {
  const guest = await call("/claim", { method: "POST", body: { ids: [UUID] } });
  assert.equal(guest.status, 401);
  assert.equal((await guest.json()).error.code, "SIGN_IN_REQUIRED");

  const bad = await call("/claim", { method: "POST", body: { ids: null }, token: "tok-ona" });
  assert.equal(bad.status, 400);
  assert.equal((await bad.json()).error.code, "INVALID_IDS");
  assert.equal(internalCalls().length, 0);

  stub.script.threads.set(UUID, { user_id: null });
  stub.script.threads.set(OTHER, { user_id: "u-somebody-else" });
  const response = await call("/claim", { method: "POST", body: { ids: [UUID, OTHER] }, token: "tok-ona" });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { claimed: 1 });
  assert.deepEqual(relayed("/internal/assistant/threads/claim").body, { ids: [UUID, OTHER], user_id: "u-ona" });
});


// ---------------------------------------------------------
// /:id/messages, /:id/feedback, /:id/delete — requireUuid
// ---------------------------------------------------------

test("a non-uuid thread id is a 404 THREAD_NOT_FOUND on every per-thread route, before any relay", async () => {
  for (const [path, method, body] of [
    ["/not-a-uuid/messages", "GET", undefined],
    ["/THREAD-1/feedback", "POST", { messageId: "m1", rating: 1 }],
    ["/12345/delete", "POST", {}],
  ]) {
    const response = await call(path, { method, body });
    assert.equal(response.status, 404, path);
    assert.equal((await response.json()).error.code, "THREAD_NOT_FOUND", path);
  }
  assert.equal(internalCalls().length, 0);
});


test("the transcript is relayed with the id lowercased, the user only when signed in", async () => {
  stub.script.threads.set(UUID, { user_id: null });
  stub.script.transcripts.set(UUID, [{ id: "m1", role: "user", content: { parts: [] } }]);

  const guest = await call(`/${UUID.toUpperCase()}/messages`);
  assert.equal(guest.status, 200);
  assert.equal((await guest.json()).messages[0].id, "m1");
  const guestRelay = relayed(`/internal/assistant/threads/${UUID}/messages`, "GET");
  assert.ok(guestRelay, "the path segment reached Django lowercased");
  assert.deepEqual(guestRelay.query, {});

  stub.requests.length = 0;
  await call(`/${UUID}/messages`, { token: "tok-ona" });
  assert.equal(relayed(`/internal/assistant/threads/${UUID}/messages`, "GET").query.user_id, "u-ona");
});


test("Django's 404 for a thread that is not the asker's relays as the phone's 404", async () => {
  stub.script.threads.set(UUID, { user_id: "u-somebody-else" });
  const response = await call(`/${UUID}/messages`, { token: "tok-ona" });
  assert.equal(response.status, 404);
  const body = await response.json();
  assert.equal(body.error.code, "DJANGO_API_ERROR");
  assert.ok(body.message, "the envelope carries a message");
});


test("feedback refuses a bad rating or a missing messageId with 400 INVALID_FEEDBACK, then relays the verdict", async () => {
  stub.script.threads.set(UUID, { user_id: null });
  for (const body of [{ messageId: "m1", rating: 2 }, { messageId: "m1", rating: "1" }, { rating: 1 }, {}]) {
    const response = await call(`/${UUID}/feedback`, { method: "POST", body });
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error.code, "INVALID_FEEDBACK");
  }
  assert.equal(internalCalls().length, 0);

  for (const rating of [1, -1, 0]) {
    const response = await call(`/${UUID}/feedback`, { method: "POST", body: { messageId: "m1", rating }, token: "tok-ona" });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).rating, rating);
  }
  const relay = relayed(`/internal/assistant/threads/${UUID}/feedback`);
  assert.deepEqual(relay.body, { user_id: "u-ona", message_id: "m1", rating: 1 });
});


test("delete relays the caller's identity and Django's answer", async () => {
  stub.script.threads.set(UUID, { user_id: "u-ona" });
  const response = await call(`/${UUID}/delete`, { method: "POST", body: {}, token: "tok-ona" });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ok: true });
  assert.deepEqual(relayed(`/internal/assistant/threads/${UUID}/delete`).body, { user_id: "u-ona" });

  // Owned by somebody else: Django's 404, relayed
  stub.script.threads.set(OTHER, { user_id: "u-somebody-else" });
  const refused = await call(`/${OTHER}/delete`, { method: "POST", body: {}, token: "tok-ona" });
  assert.equal(refused.status, 404);
});
