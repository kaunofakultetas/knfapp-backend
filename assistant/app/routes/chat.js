// -----------------------------------------------------------
//  [*] Assistant — the chat stream
//
//  POST /api/assistant/chat — the wire the mobile engine's
//  transport speaks: the AI SDK v7 chat body in (thread id
//  + UIMessages), the UI message stream (SSE) back. One
//  turn top to bottom: resolve who is asking, rate-limit,
//  validate and clamp the body, verify the thread belongs
//  to the asker, stream the model with the three tools,
//  and — after the stream closes — persist the turn's two
//  new messages and one telemetry row, both fire-and-forget
//  (a lost write must never surface as a chat failure).
//  The response head waits for the model's first word: a
//  gateway that refuses the turn (quota, dead key, 5xx) is
//  answered as the JSON envelope with the status it means,
//  not as apology text inside a 200 — and one that goes
//  SILENT before its first word is a 504. A gateway that
//  stalls mid-answer (the chunk / step / total ceilings) is
//  told apart from the student pressing Stop: the answer is
//  closed with a visible "cut off" line and an error frame
//  (the phone's banner, with Retry), the stored reply carries
//  the same line, and the telemetry says error, not aborted
//  (KNF-067).
//
//  Split into:
//
//    validateBody       — clamp sizes, 400 on nonsense
//    verifyThread       — the asker may write this thread
//    prepareMessages    — UIMessages → pruned model messages
//    awaitFirstEvent    — hold the head until the model answers
//    markStalls         — a stalled stream ends visibly
//    withStallNote      — the stored reply's "cut off" line
//    pipeWebResponse    — Web Response → Express response
//    stripToolParts     — replayed history loses tool results
//    persistTurn        — the turn's messages, upserted
//    logTurn            — one telemetry row
//    POST /             — the endpoint composing the above
// -----------------------------------------------------------

import { randomUUID } from "node:crypto";

import { Router } from "express";
import {
  convertToModelMessages, createUIMessageStreamResponse, pruneMessages, stepCountIs, streamText,
} from "ai";

import {
  CHAT_CHUNK_TIMEOUT_MS,
  CHAT_STEP_TIMEOUT_MS,
  CHAT_TIMEOUT_MS,
  MAX_BODY_CHARS,
  MAX_INPUT_CHARS,
  MAX_MESSAGES,
  MAX_STEPS,
} from "../config.js";
import { AI_CHAT_MODEL } from "../config.js";
import {
  HttpError, formatStreamError, gatewayErrorToHttpError, streamErrorToAssistantText,
} from "../middleware/errors.js";
import { buildSystemPrompt } from "../llm/prompt.js";
import { getModel, isModelConfigured } from "../llm/provider.js";
import { TOOL_SCHEMAS, createTools } from "../llm/tools.js";
import { internalFetch } from "../services/django.js";
import { resolveIdentity } from "../services/identity.js";
import { activePrompt } from "../services/prompts.js";
import { checkTurnLimit } from "../services/ratelimit.js";


const router = Router();





// -----------------------------------------------------------
// validateBody
// -----------------------------------------------------------
//
// The abuse gate before any model spend: a messages array,
// capped in count, total text size AND serialized size (the
// text cap alone let a 2 MB body of non-text parts count as
// 0 chars), roles closed to user/assistant (a client-supplied
// "system" message would override the faculty prompt), and
// part types closed per role: a user turn is TEXT ONLY — a
// `file` part would make the model layer fetch attacker URLs
// from inside the isolated network — while an assistant turn
// may carry the part types our own stream produces (its tool
// parts are the replayed history the model needs — and only
// the three tools this container runs: a `tool-<anything>`
// part is forged). `parts` must be an array when present — a
// truthy non-array used to throw a raw TypeError out of the
// loop, a 500 for a malformed request (KNF-154). The
// persisted thread is named ONLY by `threadId` — the field
// our own transport wrapper injects. The upstream chat body
// also carries an `id`, but that is assistant-ui's INTERNAL
// chat id, never one of our threads; reading it as one made
// every phone turn die on the thread check. No threadId = a
// stateless turn (streams fine, persists nothing).
//
// Used by:
//   - POST / (below)
// -----------------------------------------------------------

// The part types our own UI stream produces on an assistant
// message — anything else in a replayed history is forged
const ASSISTANT_PART_TYPES = new Set(["text", "reasoning", "step-start"]);

// The tool parts our stream can produce: one per tool this
// container actually runs
const TOOL_PART_TYPES = new Set(Object.keys(TOOL_SCHEMAS).map((name) => `tool-${name}`));

export function validateBody(body) {
  const messages = body?.messages;
  if (!Array.isArray(messages) || messages.length === 0) {
    throw new HttpError(400, "INVALID_MESSAGES", "Chat request must include messages");
  }
  if (messages.length > MAX_MESSAGES) {
    throw new HttpError(400, "TOO_MANY_MESSAGES", `At most ${MAX_MESSAGES} messages per turn`);
  }
  if (JSON.stringify(messages).length > MAX_BODY_CHARS) {
    throw new HttpError(400, "INPUT_TOO_LARGE", "Conversation too large");
  }

  let chars = 0;
  for (const message of messages) {
    const role = message?.role;
    if (role !== "user" && role !== "assistant") {
      throw new HttpError(400, "INVALID_ROLE", "Message roles are limited to user and assistant");
    }
    if (message.parts != null && !Array.isArray(message.parts)) {
      throw new HttpError(400, "INVALID_PART", "Message parts must be an array");
    }
    for (const part of message.parts || []) {
      const type = typeof part?.type === "string" ? part.type : "";
      const allowed = role === "user"
        ? type === "text"
        : ASSISTANT_PART_TYPES.has(type) || TOOL_PART_TYPES.has(type);
      if (!allowed) {
        throw new HttpError(400, "INVALID_PART", `Part type "${type}" is not accepted for ${role} messages`);
      }
      if (typeof part?.text === "string") chars += part.text.length;
    }
  }
  if (chars > MAX_INPUT_CHARS) {
    throw new HttpError(400, "INPUT_TOO_LARGE", "Conversation too large");
  }

  const threadId = typeof body?.threadId === "string" && body.threadId ? body.threadId : null;
  return { messages, threadId };
}





// -----------------------------------------------------------
// verifyThread
// -----------------------------------------------------------
//
// The access check before the model runs: the internal
// lookup answers the thread only when it is the asker's
// (or ownerless). An id the lookup does not return is a
// 404 — same cloak as everywhere: "not yours" and "not
// there" read identically.
//
// Used by:
//   - POST / (below)
// -----------------------------------------------------------

async function verifyThread(threadId, userId) {
  const answer = await internalFetch("/internal/assistant/threads/lookup", {
    method: "POST",
    body: { ids: [threadId], user_id: userId },
  });
  if (!answer?.threads?.some((thread) => thread.id === threadId)) {
    throw new HttpError(404, "THREAD_NOT_FOUND", "Thread not found");
  }
}





// -----------------------------------------------------------
// prepareMessages
// -----------------------------------------------------------
//
// UIMessages → model messages (400 when they do not parse
// — a client bug, not a 500), then old tool traffic pruned:
// the model keeps its recent tool interactions, history
// past that rides as plain text.
//
// Used by:
//   - POST / (below)
// -----------------------------------------------------------

async function prepareMessages(messages) {
  let modelMessages;
  try {
    modelMessages = await convertToModelMessages(messages);
  } catch (err) {
    throw new HttpError(400, "INVALID_UI_MESSAGES", "Failed to parse chat messages",
                        err?.message || null);
  }
  return pruneMessages({
    messages: modelMessages,
    toolCalls: "before-last-10-messages",
    emptyMessages: "remove",
  });
}





// -----------------------------------------------------------
// awaitFirstEvent
// -----------------------------------------------------------
//
//   awaitFirstEvent(uiStream, () => firstError) → chunk stream
//
// The head is not written until the model has said
// something. streamText is lazy — its UI stream exists
// before the gateway is contacted — so committing 200
// text/event-stream up front turned every gateway refusal
// (quota 429, dead virtual key, 5xx, refused connection)
// into apology text inside a success, which the phone's
// failure taxonomy can never read. Reads the UI chunk
// stream up to its first substantive chunk: `start` (the
// message id, minted before any model contact) is held; an
// `error` seen before any content is the gateway refusing
// the turn — the stream is cancelled and the failure is
// thrown as the HttpError the envelope answers. Anything
// else is the answer beginning: the held chunks are
// replayed ahead of the rest. The raw error comes from the
// caller's streamText onError hook, which fires before the
// error chunk reaches this reader; the chunk itself carries
// only the sanitized text. An `abort` before any content is
// the gateway going silent past the SDK's ceilings (a phone
// that hung up aborts too, but then nobody reads the answer)
// — 504 GATEWAY_TIMEOUT, never a 200 with nothing in it.
//
// Used by:
//   - POST / (below)
// -----------------------------------------------------------

async function awaitFirstEvent(uiStream, firstError, clientGone) {
  const reader = uiStream.getReader();
  const held = [];
  let ended = false;
  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      ended = true;
      break;
    }
    if (value.type === "error") {
      // Cancelling still runs the SDK's onEnd — persistTurn
      // sees the reply, and skips it for having no content
      await reader.cancel().catch(() => {});
      throw gatewayErrorToHttpError(firstError());
    }
    if (value.type === "abort") {
      await reader.cancel().catch(() => {});
      throw new HttpError(504, "GATEWAY_TIMEOUT", "The assistant's gateway did not answer in time",
                          { reason: clientGone() ? "client closed" : (value.reason ?? null) });
    }
    held.push(value);
    if (value.type !== "start") break;
  }

  return new ReadableStream({
    start(controller) {
      for (const chunk of held) controller.enqueue(chunk);
      if (ended) controller.close();
    },
    async pull(controller) {
      const { done, value } = await reader.read();
      if (done) controller.close();
      else controller.enqueue(value);
    },
    cancel(reason) {
      return reader.cancel(reason);
    },
  });
}





// -----------------------------------------------------------
// markStalls
// -----------------------------------------------------------
//
//   stream.pipeThrough(markStalls(() => clientGone))
//
// The SDK ends a stalled gateway (a silent gap past the chunk
// ceiling, a step or turn running past its own) with a bare
// `abort` frame — the same frame a phone's own Stop produces
// — and the phone renders whatever arrived as a FINISHED
// answer, a date broken off mid-sentence read as fact. While
// the phone is still listening, that frame is replaced: every
// text part still open is closed, a last text part says the
// answer was cut off, and an `error` frame carries the cause
// — the phone's error banner, with Retry. A phone that hung
// up gets the frame as is (nobody is reading).
//
// Used by:
//   - POST / (below) — between the first event and the pipe
// -----------------------------------------------------------

// What a cut-off answer ends with — in the bubble and in the
// stored transcript alike; both languages, like every text
// this container writes into an answer
export const STALL_NOTE = "\n\n⚠️ Atsakymas nutrūko — AI tarnyba nustojo atsakinėti. / "
  + "The answer was cut off — the AI service stopped responding.";

export function markStalls(clientGone) {
  const open = new Set();
  return new TransformStream({
    transform(chunk, controller) {
      if (chunk?.type === "text-start") open.add(chunk.id);
      if (chunk?.type === "text-end") open.delete(chunk.id);
      if (chunk?.type !== "abort" || clientGone()) {
        controller.enqueue(chunk);
        return;
      }
      for (const id of open) controller.enqueue({ type: "text-end", id });
      open.clear();
      controller.enqueue({ type: "text-start", id: "stall-note" });
      controller.enqueue({ type: "text-delta", id: "stall-note", delta: STALL_NOTE });
      controller.enqueue({ type: "text-end", id: "stall-note" });
      controller.enqueue({
        type: "error",
        errorText: "Atsakymas nutrūko: AI tarnyba neatsakė laiku. Pabandykite dar kartą. / "
          + "The answer was cut off: the AI service stopped answering. Please try again.\n\n"
          + `(${chunk.reason || "timeout"})`,
      });
    },
  });
}





// -----------------------------------------------------------
// withStallNote
// -----------------------------------------------------------
//
// The stored copy of a cut-off reply, ending in the same
// STALL_NOTE part the phone was shown — so a thread opened
// later never replays the broken answer as a finished one. A
// reply that never said anything stays empty (and unstored):
// that turn was a 504, and the phone's banner told it.
//
// Used by:
//   - POST / (below) — onEnd, for a stalled turn
// -----------------------------------------------------------

function withStallNote(reply) {
  if (!reply || !Array.isArray(reply.parts)) return reply;
  if (!reply.parts.some((part) => part?.type !== "step-start")) return reply;
  return { ...reply, parts: [...reply.parts, { type: "text", text: STALL_NOTE, state: "done" }] };
}





// -----------------------------------------------------------
// pipeWebResponse
// -----------------------------------------------------------
//
// The AI SDK hands back a Web Response whose body is the
// SSE stream; Express wants writes. Copy status + headers,
// pump chunks, always end — a client that vanished
// mid-answer is a log line, not a crash. The head goes out
// here, and only here — by now awaitFirstEvent has proven
// the model is answering.
//
// Used by:
//   - POST / (below)
// -----------------------------------------------------------

async function pipeWebResponse(webResponse, res) {
  const headers = {};
  webResponse.headers.forEach((value, key) => { headers[key] = value; });
  res.writeHead(webResponse.status, headers);

  try {
    const reader = webResponse.body.getReader();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      res.write(Buffer.from(value));
    }
  } catch (err) {
    console.error("Stream pipe error:", err?.message);
  }
  res.end();
}





// -----------------------------------------------------------
// stripToolParts
// -----------------------------------------------------------
//
// A replayed assistant message minus its tool parts. The
// phone's copy of an old answer is the phone's word, not
// ours: its tool results may be anything the client wrote,
// and stored as-is they became fake "sources" in the faculty
// transcript (KNF-068). This turn's OWN reply keeps its tool
// parts — the SDK built it from the calls it really made.
//
// Used by:
//   - persistTurn (below)
// -----------------------------------------------------------

function stripToolParts(message) {
  if (message?.role !== "assistant" || !Array.isArray(message.parts)) return message;
  const parts = message.parts.filter((part) => !String(part?.type || "").startsWith("tool-"));
  return parts.length === message.parts.length ? message : { ...message, parts };
}





// -----------------------------------------------------------
// persistTurn
// -----------------------------------------------------------
//
// The turn's content into Django: the TAIL of the request's
// messages plus the streamed assistant message. Sending the
// tail — not just the last pair — is the self-healing
// property: a turn whose fire-and-forget write failed is
// replayed by the NEXT turn's batch, because the client's
// request always carries the history. Django only CREATES
// rows the thread lacks and updates exactly one existing row,
// `reply_id` — this turn's reply, which a tool round's
// continuation grows in place; a replay never rewrites the
// stored history, and the tail's assistant messages arrive
// stripped of tool parts (stripToolParts). Django caps a
// batch at 20; the tail plus the reply stays inside it. A
// reply with no content — nothing past the step markers, the
// shape of a turn the gateway refused — is not stored: the
// SDK's onEnd fires for a cancelled stream too, and it used
// to write a permanent blank bubble into the thread.
// `beganOwnerless` marks a guest turn whose thread was
// verified ownerless when it started: a login that claims
// the thread mid-answer must not cost the answer (Django
// accepts that one write into the claimed thread).
// `replacedId` is the answer a regenerate (or the error
// strip's Retry) replaced — sent only with a reply that has
// content, so Django drops the old answer from the linear
// transcript instead of replaying both one after the other;
// a failed regenerate keeps the old one.
//
// Used by:
//   - POST / (below) — toUIMessageStream onEnd
// -----------------------------------------------------------

// How many trailing request messages ride in each persistence
// batch — enough to backfill a previously failed turn or two
const PERSIST_TAIL = 10;

function persistTurn(threadId, userId, requestMessages, responseMessage, { beganOwnerless = false, replacedId = null } = {}) {
  const tail = requestMessages
    .filter((message) => message?.id && (message.role === "user" || message.role === "assistant"))
    .slice(-PERSIST_TAIL)
    .filter((message) => message.id !== responseMessage?.id)
    .map(stripToolParts);
  const hasContent = Array.isArray(responseMessage?.parts)
    && responseMessage.parts.some((part) => part?.type !== "step-start");
  const reply = responseMessage?.id && responseMessage.role && hasContent ? responseMessage : null;
  const batch = [...tail, ...(reply ? [reply] : [])];
  if (!threadId || batch.length === 0) return;

  internalFetch(`/internal/assistant/threads/${threadId}/messages`, {
    method: "POST",
    body: {
      user_id: userId,
      messages: batch,
      ...(reply ? { reply_id: reply.id } : {}),
      ...(reply && replacedId && replacedId !== reply.id ? { replaced_id: replacedId } : {}),
      ...(beganOwnerless ? { began_ownerless: true } : {}),
    },
  }).catch((err) => console.error("Turn persistence failed:", err?.message));
}





// -----------------------------------------------------------
// logTurn
// -----------------------------------------------------------
//
// One assistant_turns row per turn — usage, tool timings,
// outcome. Telemetry never throws back into the request.
// toolCalls comes from the tool layer's own recorder (real
// ok/ms per call — the steps array knew only that a call was
// ISSUED, so every failed tool logged ok:true and ms never).
//
// Used by:
//   - POST / (below) — streamText onFinish / catch paths
// -----------------------------------------------------------

function logTurn({ threadId, userId, language, clientVersion, usage, toolCalls, startedAt, outcome, promptVersion = null }) {
  internalFetch("/internal/assistant/turn-log", {
    method: "POST",
    body: {
      thread_id: threadId,
      user_id: userId,
      language,
      client_version: clientVersion,
      model: AI_CHAT_MODEL,
      prompt_version: promptVersion,
      input_tokens: usage?.inputTokens ?? 0,
      output_tokens: usage?.outputTokens ?? 0,
      tool_calls: toolCalls || [],
      duration_ms: Date.now() - startedAt,
      outcome,
    },
  }).catch((err) => console.error("Turn log failed:", err?.message));
}





// -----------------------------------------------------------
// POST / — one chat turn, streamed
// -----------------------------------------------------------
//
// The composition: identity → rate limit → validation →
// thread check → streamText with the three tools → the
// first model event awaited → UI message stream out;
// persistence and telemetry hang off the two finish hooks.
// An unconfigured gateway is a clean 503 the engine reads
// as 'unavailable', a gateway refusing the turn is the
// status it means (429 quota, 503 auth, 502/504 the rest)
// — never a stream that dies mid-first-token.
//
// Used by:
//   - the mobile engine's createKnfAssistantTransport
// -----------------------------------------------------------

router.post("/", async (req, res, next) => {
  try {
    const { userId, studyGroup, studyProgram } = await resolveIdentity(req);
    const clientVersion = req.get("x-knf-assistant-client") || null;
    const language = (req.get("accept-language") || "lt").slice(0, 2) === "en" ? "en" : "lt";

    if (!isModelConfigured()) {
      throw new HttpError(503, "NOT_CONFIGURED", "Assistant is not configured yet");
    }
    checkTurnLimit(userId ? `user:${userId}` : `ip:${req.ip}`);

    const { messages, threadId } = validateBody(req.body);
    // A regenerate names the answer it replaces
    const replacedId = req.body?.trigger === "regenerate-message" && typeof req.body?.messageId === "string"
      ? req.body.messageId
      : null;
    if (threadId) {
      await verifyThread(threadId, userId);
    }

    const startedAt = Date.now();
    const modelMessages = await prepareMessages(messages);

    // The prompt lives only in the database — no active
    // version means the assistant is deliberately OFF, and
    // that must read as a clear 503, never a promptless run.
    // (A store that could not be asked is activePrompt's own
    // 503 PROMPT_UNAVAILABLE — a different message, so nobody
    // is sent to activate a prompt that is active.)
    const prompt = await activePrompt();
    if (!prompt.text) {
      throw new HttpError(503, "PROMPT_NOT_CONFIGURED",
                          "No active system prompt — activate one in the admin panel");
    }

    // Exactly one telemetry row per turn, whichever hook fires
    // first — a fatal stream error must not go uncounted, and
    // an error followed by a finish must not count twice. The
    // tool layer records its calls into toolCalls (real ok/ms),
    // and finished steps accumulate usage into `spent` so an
    // errored or aborted turn still logs the tokens it burned
    // instead of a flat zero.
    let turnLogged = false;
    const toolCalls = [];
    const spent = { inputTokens: 0, outputTokens: 0 };
    // The first raw model error — the SDK's UI stream carries
    // only its text, and awaitFirstEvent needs the class and
    // status to answer a refusal with the right envelope
    let firstStreamError = null;
    const logOnce = (outcome, usage = null) => {
      if (turnLogged) return;
      turnLogged = true;
      logTurn({ threadId, userId, language, clientVersion, usage: usage ?? spent, toolCalls,
                startedAt, outcome, promptVersion: prompt.version });
    };

    // A phone that hung up mid-answer stops the model too —
    // without this the stream ran its full 90 s for nobody,
    // billed in full, and the 'aborted' outcome never fired.
    // res 'close' also follows a NORMAL end, so only an
    // unfinished response counts as a walk-away — and the flag
    // tells that walk-away apart from the SDK's own ceilings
    // aborting a stalled gateway (`stalled`, set in onAbort)
    const abort = new AbortController();
    let clientGone = false;
    let stalled = false;
    res.on("close", () => {
      if (res.writableEnded) return;
      clientGone = true;
      abort.abort();
    });

    const result = streamText({
      model: getModel(),
      system: buildSystemPrompt(language, { studyGroup, studyProgram }, prompt.text),
      messages: modelMessages,
      tools: createTools({ language, record: (call) => toolCalls.push(call) }),
      maxRetries: 0,
      abortSignal: abort.signal,
      timeout: {
        totalMs: CHAT_TIMEOUT_MS,
        stepMs: CHAT_STEP_TIMEOUT_MS,
        chunkMs: CHAT_CHUNK_TIMEOUT_MS,
      },
      stopWhen: stepCountIs(MAX_STEPS),
      onStepFinish: ({ usage }) => {
        spent.inputTokens += usage?.inputTokens ?? 0;
        spent.outputTokens += usage?.outputTokens ?? 0;
      },
      onError: ({ error }) => {
        firstStreamError ??= error;
        console.error("Chat stream error:", formatStreamError(error));
        logOnce("error");
      },
      onFinish: ({ usage }) => {
        logOnce("ok", usage);
      },
      onAbort: () => {
        // Only the student's own Stop is an 'aborted' turn — a
        // gateway gone silent is an error the operator must see
        stalled = !clientGone;
        logOnce(clientGone ? "aborted" : "error");
      },
    });

    const uiStream = result.toUIMessageStream({
      originalMessages: messages,
      // Without this the response message carries NO id when
      // the last original message is the user's — and an
      // id-less reply cannot be persisted or rated
      generateMessageId: () => `srv-${randomUUID()}`,
      onError: (error) => streamErrorToAssistantText(error),
      // The finish payload's shape moved across ai v7 minors —
      // some hand the new message as `responseMessage`, some
      // only the full updated `messages` list. Take either.
      onEnd: ({ messages: finished, responseMessage }) => {
        const reply = responseMessage
          ?? [...(finished ?? [])].reverse().find((message) => message?.role === "assistant");
        persistTurn(threadId, userId, messages, stalled ? withStallNote(reply) : reply,
                    { beganOwnerless: Boolean(threadId) && !userId, replacedId });
      },
    });
    // The same Web Response toUIMessageStreamResponse builds —
    // headers, SSE framing — over the stream once its first
    // event has proven the model is answering; a stall after
    // that ends the answer visibly (markStalls)
    const stream = await awaitFirstEvent(uiStream, () => firstStreamError, () => clientGone);
    const marked = stream.pipeThrough(markStalls(() => clientGone));
    await pipeWebResponse(createUIMessageStreamResponse({ stream: marked }), res);
  } catch (err) {
    // Retry-After tells the engine's quota / unavailable
    // failure how long the person actually waits — the turn
    // limiter's window, or the gateway's own header relayed
    if (err instanceof HttpError && err.details?.retryAfterS != null) {
      res.set("Retry-After", String(err.details.retryAfterS));
    }
    next(err);
  }
});

export default router;
