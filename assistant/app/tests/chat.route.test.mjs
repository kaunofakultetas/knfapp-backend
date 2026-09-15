// -----------------------------------------------------------
//  [*] Tests — the chat route, whole
//
//  The REAL route top to bottom — real Express app, real
//  identity/rate-limit/validation/thread-check, real tools,
//  real persistence and telemetry fetches — with only the
//  two externals faked: Django (the scripted stub server,
//  DJANGO_URL pointed at it) and the model (a scripted
//  MockLanguageModelV4 through provider.js's test seam).
//  Everything the live eval exercises expensively and
//  nondeterministically, this suite pins for free: the SSE
//  out, the exact persistence batch, the telemetry row, the
//  error envelope the mobile failure taxonomy parses, the
//  429's Retry-After, the abort path.
//
//    docker exec knfapp-assistant npm test
//
//  Ordering notes: each test uses its OWN identity (token or
//  X-Forwarded-For) so the in-memory rate limiter never
//  couples cases, and the prompt cache is reset per test.
// -----------------------------------------------------------

import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";

import { startStubDjango, waitFor, STUB_SECRET } from "./stubDjango.mjs";


// The stub must exist before any app import — config.js
// reads these once, at first import
const stub = await startStubDjango();
process.env.DJANGO_URL = stub.url;
process.env.ASSISTANT_INTERNAL_SECRET = STUB_SECRET;
process.env.AI_GATEWAY_URL = "http://127.0.0.1:1/v1";
process.env.AI_GATEWAY_KEY = "test-key";

const { default: express } = await import("express");
const { MockLanguageModelV4, simulateReadableStream } = await import("ai/test");
const { default: chatRoutes } = await import("../routes/chat.js");
const { errorMiddleware } = await import("../middleware/errors.js");
const { setModelForTests } = await import("../llm/provider.js");
const { resetPromptCacheForTests } = await import("../services/prompts.js");
const { RATE_LIMIT_TURNS } = await import("../config.js");

// The same three lines server.js gives the app
const app = express();
app.set("trust proxy", true);
app.use(express.json({ limit: "2mb" }));
app.use("/api/assistant/chat", chatRoutes);
app.use(errorMiddleware);
const server = app.listen(0, "127.0.0.1");
await new Promise((resolve) => server.once("listening", resolve));
const BASE = `http://127.0.0.1:${server.address().port}/api/assistant/chat`;

after(async () => {
  server.close();
  await stub.close();
});


// ---------------------------------------------------------
// Script builders
// ---------------------------------------------------------

const USAGE = { inputTokens: { total: 11 }, outputTokens: { total: 7 } };
const finish = (unified) => ({ type: "finish", finishReason: { unified, raw: unified }, usage: USAGE });

const textStream = (text, { delayMs } = {}) => simulateReadableStream({
  chunkDelayInMs: delayMs ?? 0,
  chunks: [
    { type: "text-start", id: "t1" },
    ...[...text].map((ch) => ({ type: "text-delta", id: "t1", delta: ch })),
    { type: "text-end", id: "t1" },
    finish("stop"),
  ],
});

// A model answering plain text, capturing what it was asked
function scriptTextModel(text, options = {}) {
  const seen = { calls: [] };
  setModelForTests(new MockLanguageModelV4({
    doStream: async (call) => {
      seen.calls.push(call);
      return { stream: textStream(text, options) };
    },
  }));
  return seen;
}

// A model calling one tool, then answering with text
function scriptToolModel(toolName, input, answer) {
  const seen = { calls: [] };
  setModelForTests(new MockLanguageModelV4({
    doStream: async (call) => {
      seen.calls.push(call);
      if (seen.calls.length === 1) {
        return { stream: simulateReadableStream({ chunks: [
          { type: "tool-call", toolCallId: "call-1", toolName, input: JSON.stringify(input) },
          finish("tool-calls"),
        ]}) };
      }
      return { stream: textStream(answer) };
    },
  }));
  return seen;
}

let nextIp = 0;
const ask = (body, { token, headers = {} } = {}) => fetch(BASE, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    // A fresh guest identity per request unless a test pins one
    "X-Forwarded-For": headers["X-Forwarded-For"] ?? `10.99.0.${(nextIp += 1)}`,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...headers,
  },
  body: JSON.stringify(body),
});

const userTurn = (text, threadId = undefined) => ({
  id: "chat-internal-id",
  ...(threadId !== undefined ? { threadId } : {}),
  messages: [{ id: "u1", role: "user", parts: [{ type: "text", text }] }],
  trigger: "submit-message",
});

// The SSE body parsed into frames
async function frames(response) {
  const raw = await response.text();
  return raw.split("\n")
    .filter((line) => line.startsWith("data: ") && !line.includes("[DONE]"))
    .map((line) => JSON.parse(line.slice(6)));
}

const streamedText = (parsed) => parsed
  .filter((frame) => frame.type === "text-delta")
  .map((frame) => frame.delta).join("");

const turnLogs = () => stub.requests.filter((r) => r.path === "/internal/assistant/turn-log");
const persists = () => stub.requests.filter((r) => /\/messages$/.test(r.path) && r.method === "POST");


beforeEach(() => {
  stub.requests.length = 0;
  stub.script.prompt = { version: 7, text: "CORE {TODAY} ({WEEKDAY}) lang={LEADING_LANGUAGE}\n{USER_CONTEXT}" };
  stub.script.search = { status: 200, results: [] };
  resetPromptCacheForTests();
});


// ---------------------------------------------------------
// The turns
// ---------------------------------------------------------

test("a guest turn streams text and logs one truthful telemetry row", async () => {
  scriptTextModel("Labas, studente!");
  const response = await ask(userTurn("Labas"));
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") || "", /text\/event-stream/);
  assert.equal(streamedText(await frames(response)), "Labas, studente!");

  const log = await waitFor(() => turnLogs()[0]);
  assert.equal(log.body.outcome, "ok");
  assert.equal(log.body.user_id, null);
  assert.equal(log.body.prompt_version, 7);
  assert.equal(log.body.input_tokens, 11);
  assert.equal(log.body.output_tokens, 7);
  assert.deepEqual(log.body.tool_calls, []);
  assert.equal(turnLogs().length, 1);
  // No threadId — a stateless turn persists nothing
  assert.equal(persists().length, 0);
});


test("the rendered system prompt reaches the model: date, language, profile — no raw placeholders", async () => {
  stub.script.users.set("tok-render", { id: "u-render", studyGroup: 'EV"2\ninjected', studyProgram: "Informacijos sistemos" });
  const seen = scriptTextModel("ok");
  await (await ask(userTurn("kada paskaitos?"), { token: "tok-render" })).text();

  const system = seen.calls[0].prompt.find((message) => message.role === "system");
  assert.ok(system, "system message reaches the model");
  const content = typeof system.content === "string"
    ? system.content
    : system.content.map((part) => part.text ?? "").join("");
  assert.match(content, /CORE \d{4}-\d{2}-\d{2} \((Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\) lang=Lithuanian/);
  // The profile is IN, the injection characters are NOT: the
  // quote and the newline folded to spaces inside the value
  assert.match(content, /study group "EV 2 injected"/);
  assert.ok(!content.includes('EV"2'), "quotes stripped from profile fields");
  assert.ok(!content.includes("2\ninjected"), "newlines stripped from profile fields");
  assert.ok(!content.includes("{USER_CONTEXT}"), "no unrendered placeholder");
  // The fire-and-forget log must land INSIDE this test — a
  // late arrival would bleed into the next case's assertions
  await waitFor(() => turnLogs()[0]);
});


test("a thread turn persists the tail plus the server-minted reply", async () => {
  stub.script.users.set("tok-persist", { id: "u-persist" });
  stub.script.threads.set("thread-1", { user_id: "u-persist" });
  scriptTextModel("Atsakymas čia.");

  const response = await ask(userTurn("Klausimas", "thread-1"), { token: "tok-persist" });
  assert.equal(response.status, 200);
  await response.text();

  const persisted = await waitFor(() => persists()[0]);
  assert.match(persisted.path, /threads\/thread-1\/messages$/);
  assert.equal(persisted.body.user_id, "u-persist");
  const batch = persisted.body.messages;
  assert.equal(batch[0].id, "u1");
  assert.equal(batch[0].role, "user");
  const reply = batch[batch.length - 1];
  assert.equal(reply.role, "assistant");
  assert.match(reply.id, /^srv-/, "the reply id is server-minted");
  assert.equal(reply.parts.filter((part) => part.type === "text").map((part) => part.text).join(""), "Atsakymas čia.");

  const log = await waitFor(() => turnLogs().find((r) => r.body.thread_id === "thread-1"));
  assert.equal(log.body.user_id, "u-persist");
});


test("a thread that is not the asker's is a 404 before any model spend", async () => {
  stub.script.users.set("tok-mallory", { id: "u-mallory" });
  stub.script.threads.set("thread-owned", { user_id: "u-somebody-else" });
  const seen = scriptTextModel("never");

  const response = await ask(userTurn("labas", "thread-owned"), { token: "tok-mallory" });
  assert.equal(response.status, 404);
  const body = await response.json();
  assert.equal(body.error.code, "THREAD_NOT_FOUND");
  assert.equal(seen.calls.length, 0, "the model was never called");
});


// ---------------------------------------------------------
// The failure envelopes the mobile taxonomy parses
// ---------------------------------------------------------

test("an invalid bearer is a 401 SESSION_INVALID envelope", async () => {
  scriptTextModel("never");
  const response = await ask(userTurn("labas"), { token: "tok-nobody" });
  assert.equal(response.status, 401);
  const body = await response.json();
  assert.equal(body.error.code, "SESSION_INVALID");
  assert.ok(body.message, "envelope carries a human message");
});


test("no active prompt is a 503 PROMPT_NOT_CONFIGURED, never a promptless run", async () => {
  stub.script.prompt = { version: null, text: "" };
  const seen = scriptTextModel("never");
  const response = await ask(userTurn("labas"));
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error.code, "PROMPT_NOT_CONFIGURED");
  assert.equal(seen.calls.length, 0);
});


test("the guest rate limit answers 429 with a real Retry-After", async () => {
  scriptTextModel("ok");
  const sameIp = { "X-Forwarded-For": "10.99.100.1" };
  for (let turn = 0; turn < RATE_LIMIT_TURNS; turn += 1) {
    const response = await ask(userTurn(`k${turn}`), { headers: sameIp });
    assert.equal(response.status, 200);
    await response.text();
  }
  const limited = await ask(userTurn("per daug"), { headers: sameIp });
  assert.equal(limited.status, 429);
  assert.equal((await limited.json()).error.code, "RATE_LIMITED");
  const retryAfter = Number(limited.headers.get("retry-after"));
  assert.ok(retryAfter >= 1 && retryAfter <= 61, `Retry-After is a real wait (${retryAfter})`);
  // Drain this burst's fire-and-forget logs before the next case
  await waitFor(() => turnLogs().length >= RATE_LIMIT_TURNS);
});


// ---------------------------------------------------------
// Tools inside the stream
// ---------------------------------------------------------

test("a searchHandbook round-trip streams the tool frames and logs real ok/ms", async () => {
  stub.script.search = { status: 200, results: [
    { id: "curated:lt:abc", title: "Biblioteka", excerpt: "Darbo laikas 8–20", section: "hours", language: "lt" },
  ]};
  scriptToolModel("searchHandbook", { query: "bibliotekos darbo laikas" }, "Biblioteka dirba 8–20 [1].");

  const response = await ask(userTurn("Koks bibliotekos darbo laikas?"));
  assert.equal(response.status, 200);
  const parsed = await frames(response);
  assert.ok(parsed.some((frame) => frame.type === "tool-input-available"
                                && frame.toolName === "searchHandbook"), "tool input frame streamed");
  const output = parsed.find((frame) => frame.type === "tool-output-available");
  assert.equal(output.output.entries[0].title, "Biblioteka");
  assert.equal(streamedText(parsed), "Biblioteka dirba 8–20 [1].");

  // The tool asked Django with the request's language attached
  const search = stub.requests.find((r) => r.path === "/internal/assistant/search");
  assert.equal(search.body.query, "bibliotekos darbo laikas");
  assert.equal(search.body.language, "lt");

  const log = await waitFor(() => turnLogs().find((r) => r.body.tool_calls?.length === 1));
  assert.equal(log.body.outcome, "ok");
  const [callRow] = log.body.tool_calls;
  assert.equal(callRow.name, "searchHandbook");
  assert.equal(callRow.ok, true);
  assert.equal(typeof callRow.ms, "number");
});


test("a failing tool logs ok:false and the turn still answers", async () => {
  stub.script.search = { status: 502 };
  scriptToolModel("searchHandbook", { query: "x" }, "Atsiprašau, žinynas nepasiekiamas.");

  const response = await ask(userTurn("Koks darbo laikas?"));
  assert.equal(response.status, 200);
  const parsed = await frames(response);
  assert.ok(parsed.some((frame) => frame.type === "tool-output-error"), "the failure reaches the stream as a tool error");
  assert.equal(streamedText(parsed), "Atsiprašau, žinynas nepasiekiamas.");

  const log = await waitFor(() => turnLogs().find((r) => r.body.tool_calls?.length === 1));
  assert.equal(log.body.outcome, "ok", "a tool failure is not a turn failure");
  assert.equal(log.body.tool_calls[0].ok, false);
});


// ---------------------------------------------------------
// The two abnormal ends
// ---------------------------------------------------------

test("a client hang-up aborts the model run and logs outcome aborted", async () => {
  scriptTextModel("Ilgas atsakymas kuris niekada nebus baigtas skaityti", { delayMs: 40 });

  const controller = new AbortController();
  const response = await ask(userTurn("labas"));
  const reader = response.body.getReader();
  await reader.read();
  // The phone goes away mid-stream
  await reader.cancel();
  controller.abort();

  const log = await waitFor(() => turnLogs().find((r) => r.body.outcome === "aborted"),
                            { timeoutMs: 5000 });
  assert.ok(log, "the walk-away turn was counted as aborted");
});


test("a mid-stream model error folds into apologetic text WITH the cause, and logs an error outcome", async () => {
  setModelForTests(new MockLanguageModelV4({
    doStream: async () => ({
      stream: simulateReadableStream({ chunks: [
        { type: "text-start", id: "t1" },
        { type: "text-delta", id: "t1", delta: "Prad" },
        { type: "error", error: new Error("gateway exploded mid-answer") },
      ]}),
    }),
  }));

  const response = await ask(userTurn("labas"));
  assert.equal(response.status, 200, "the stream already started — the failure rides inside it");
  const raw = (await frames(response)).map((frame) => JSON.stringify(frame)).join("\n");
  assert.match(raw, /Atsiprašau, įvyko klaida/);
  // The cause stays visible on purpose — students debug by screenshot
  assert.match(raw, /gateway exploded mid-answer/);

  const log = await waitFor(() => turnLogs().find((r) => r.body.outcome === "error"));
  assert.ok(log, "the failed turn was counted as an error");
});
