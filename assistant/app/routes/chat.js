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
//  not as apology text inside a 200.
//
//  Split into:
//
//    validateBody       — clamp sizes, 400 on nonsense
//    verifyThread       — the asker may write this thread
//    prepareMessages    — UIMessages → pruned model messages
//    awaitFirstEvent    — hold the head until the model answers
//    pipeWebResponse    — Web Response → Express response
//    persistTurn        — the two new messages, upserted
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
import { createTools } from "../llm/tools.js";
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
// parts are the replayed history the model needs). The
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
    for (const part of message?.parts || []) {
      const type = typeof part?.type === "string" ? part.type : "";
      const allowed = role === "user"
        ? type === "text"
        : ASSISTANT_PART_TYPES.has(type) || type.startsWith("tool-");
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
// only the sanitized text.
//
// Used by:
//   - POST / (below)
// -----------------------------------------------------------

async function awaitFirstEvent(uiStream, firstError) {
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
// persistTurn
// -----------------------------------------------------------
//
// The turn's content into Django: the TAIL of the request's
// messages plus the streamed assistant message, upserted by
// id. Sending the tail — not just the last pair — is the
// self-healing property: a turn whose fire-and-forget write
// failed is replayed by the NEXT turn's batch, because the
// client's request always carries the history and the
// upsert is idempotent. Django caps a batch at 20; the tail
// plus the reply stays inside it. A reply with no content —
// nothing past the step markers, the shape of a turn the
// gateway refused — is not stored: the SDK's onEnd fires for
// a cancelled stream too, and it used to write a permanent
// blank bubble into the thread.
//
// Used by:
//   - POST / (below) — toUIMessageStream onEnd
// -----------------------------------------------------------

// How many trailing request messages ride in each persistence
// batch — enough to backfill a previously failed turn or two
const PERSIST_TAIL = 10;

function persistTurn(threadId, userId, requestMessages, responseMessage) {
  const tail = requestMessages
    .filter((message) => message?.id && (message.role === "user" || message.role === "assistant"))
    .slice(-PERSIST_TAIL);
  const hasContent = Array.isArray(responseMessage?.parts)
    && responseMessage.parts.some((part) => part?.type !== "step-start");
  const batch = [...tail.filter((message) => message.id !== responseMessage?.id),
                 ...(responseMessage?.id && responseMessage.role && hasContent ? [responseMessage] : [])];
  if (!threadId || batch.length === 0) return;

  internalFetch(`/internal/assistant/threads/${threadId}/messages`, {
    method: "POST",
    body: { user_id: userId, messages: batch },
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
    // unfinished response counts as a walk-away.
    const abort = new AbortController();
    res.on("close", () => {
      if (!res.writableEnded) abort.abort();
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
        logOnce("aborted");
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
        persistTurn(threadId, userId, messages, reply);
      },
    });
    // The same Web Response toUIMessageStreamResponse builds —
    // headers, SSE framing — over the stream once its first
    // event has proven the model is answering
    const stream = await awaitFirstEvent(uiStream, () => firstStreamError);
    await pipeWebResponse(createUIMessageStreamResponse({ stream }), res);
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
