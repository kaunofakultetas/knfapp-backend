// -----------------------------------------------------------
//  [*] Tests — a gateway that goes silent (KNF-067)
//
//  The chat route under SHORT ceilings (chunk 300 ms, step
//  1.5 s, turn 3 s — set before the app is imported, so this
//  file runs in its own process), against a model that says a
//  few words and then holds the socket open saying nothing.
//  Mid-answer the SDK aborts with the same bare frame a
//  phone's Stop produces; the route must not let that read as
//  a finished answer: the partial text stays, a "cut off" text
//  part closes it, an error frame carries the cause (the
//  phone's banner, with Retry), the stored reply ends with the
//  same line, and the telemetry says error — not aborted.
//  Silent from the very start is a 504 GATEWAY_TIMEOUT with no
//  reply stored. A phone that hangs up is still 'aborted'.
//
//    docker exec knfapp-assistant npm test
// -----------------------------------------------------------

import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";

import { startStubDjango, waitFor, STUB_SECRET } from "./stubDjango.mjs";


const stub = await startStubDjango();
process.env.DJANGO_URL = stub.url;
process.env.ASSISTANT_INTERNAL_SECRET = STUB_SECRET;
process.env.AI_GATEWAY_URL = "http://127.0.0.1:1/v1";
process.env.AI_GATEWAY_KEY = "test-key";
process.env.CHAT_CHUNK_TIMEOUT_MS = "300";
process.env.CHAT_STEP_TIMEOUT_MS = "1500";
process.env.CHAT_TIMEOUT_MS = "3000";

const { default: express } = await import("express");
const { MockLanguageModelV4 } = await import("ai/test");
const { default: chatRoutes, STALL_NOTE } = await import("../routes/chat.js");
const { errorMiddleware } = await import("../middleware/errors.js");
const { setModelForTests } = await import("../llm/provider.js");
const { resetPromptCacheForTests } = await import("../services/prompts.js");

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


// A model that emits `chunks` and then NOTHING — the socket
// stays open until the SDK's own ceiling aborts the call
function scriptStallingModel(chunks) {
  setModelForTests(new MockLanguageModelV4({
    doStream: async ({ abortSignal }) => ({
      stream: new ReadableStream({
        start(controller) {
          for (const chunk of chunks) controller.enqueue(chunk);
          abortSignal?.addEventListener("abort", () => {
            try {
              controller.error(Object.assign(new Error("aborted"), { name: "AbortError" }));
            } catch {
              // Already closed
            }
          }, { once: true });
        },
      }),
    }),
  }));
}

let nextIp = 0;
const ask = (threadId, { token } = {}) => fetch(BASE, {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-Forwarded-For": `10.77.0.${(nextIp += 1)}`,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  },
  body: JSON.stringify({
    id: "chat-internal-id",
    ...(threadId ? { threadId } : {}),
    messages: [{ id: "u1", role: "user", parts: [{ type: "text", text: "Kada egzaminai?" }] }],
    trigger: "submit-message",
  }),
});

const frames = async (response) => (await response.text()).split("\n")
  .filter((line) => line.startsWith("data: ") && !line.includes("[DONE]"))
  .map((line) => JSON.parse(line.slice(6)));

const turnLogs = () => stub.requests.filter((r) => r.path === "/internal/assistant/turn-log");
const persists = () => stub.requests.filter((r) => /\/messages$/.test(r.path) && r.method === "POST");


beforeEach(() => {
  stub.requests.length = 0;
  stub.script.prompt = { version: 7, text: "CORE {TODAY}" };
  resetPromptCacheForTests();
});


test("a gateway that stalls MID-answer ends it visibly: the partial text, a cut-off line, an error frame", async () => {
  stub.script.users.set("tok-stall", { id: "u-stall" });
  stub.script.threads.set("thread-stall", { user_id: "u-stall" });
  scriptStallingModel([
    { type: "text-start", id: "t1" },
    { type: "text-delta", id: "t1", delta: "Egzaminai prasideda " },
    { type: "text-delta", id: "t1", delta: "sausio " },
  ]);

  const response = await ask("thread-stall", { token: "tok-stall" });
  assert.equal(response.status, 200, "the answer had begun — the head was already out");
  const parsed = await frames(response);
  const types = parsed.map((frame) => frame.type);

  assert.ok(!types.includes("abort"), "the bare abort frame never reaches the phone");
  const text = parsed.filter((frame) => frame.type === "text-delta").map((frame) => frame.delta).join("");
  assert.equal(text, `Egzaminai prasideda sausio ${STALL_NOTE}`);
  // The open text part is closed before the note opens its own
  assert.ok(types.indexOf("text-end") < types.lastIndexOf("text-start"));
  const error = parsed.find((frame) => frame.type === "error");
  assert.ok(error, "an error frame — the phone's banner, with Retry");
  assert.match(error.errorText, /Atsakymas nutrūko/);
  assert.match(error.errorText, /timeout/i, "the cause is named for the screenshot");

  // Stored WITH the same line — replay never shows it finished
  const persisted = await waitFor(() => persists().find((r) => /thread-stall/.test(r.path)), { timeoutMs: 4000 });
  const reply = persisted.body.messages.at(-1);
  assert.equal(reply.role, "assistant");
  assert.equal(reply.parts.at(-1).text, STALL_NOTE);
  assert.equal(persisted.body.reply_id, reply.id);

  const log = await waitFor(() => turnLogs().find((r) => r.body.thread_id === "thread-stall"), { timeoutMs: 4000 });
  assert.equal(log.body.outcome, "error", "a stalled gateway is an error, not a student pressing Stop");
});


test("a gateway silent from the START is a 504 GATEWAY_TIMEOUT — no empty 200, no reply stored", async () => {
  stub.script.users.set("tok-mute", { id: "u-mute" });
  stub.script.threads.set("thread-mute", { user_id: "u-mute" });
  scriptStallingModel([]);

  const response = await ask("thread-mute", { token: "tok-mute" });
  assert.equal(response.status, 504);
  const body = await response.json();
  assert.equal(body.error.code, "GATEWAY_TIMEOUT");

  const persisted = await waitFor(() => persists().find((r) => /thread-mute/.test(r.path)), { timeoutMs: 4000 });
  assert.deepEqual(persisted.body.messages.map((message) => message.role), ["user"],
                   "the question is kept, no answer-less reply is invented");
  assert.equal(persisted.body.reply_id, undefined);
});


test("a phone that hangs up is still 'aborted' — only the SDK's own ceilings read as a stall", async () => {
  scriptStallingModel([
    { type: "text-start", id: "t1" },
    { type: "text-delta", id: "t1", delta: "Pradžia" },
  ]);
  const response = await ask(null);
  const reader = response.body.getReader();
  await reader.read();
  await reader.cancel();

  const log = await waitFor(() => turnLogs().find((r) => r.body.outcome === "aborted"), { timeoutMs: 4000 });
  assert.ok(log);
});
