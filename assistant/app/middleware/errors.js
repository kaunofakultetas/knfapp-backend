// -----------------------------------------------------------
//  [*] Assistant — error handling
//
//  One failure discipline for the whole container. Routes
//  throw HttpError; the middleware serializes every throw
//  into the JSON envelope the mobile engine's failure
//  taxonomy parses ({ message, error: { code } } — 401/403
//  read as auth, 429 as quota, 502-504 as unavailable). A
//  gateway that refuses a turn before the answer starts is
//  translated INTO that table (gatewayErrorToHttpError), so a
//  quota or a dead virtual key reaches the phone as the
//  status it means. Mid-stream model errors never break the
//  SSE stream: streamErrorToAssistantText folds them into
//  apologetic assistant text instead — with the cause named,
//  but never the gateway's own words (its error body carries
//  the virtual-key identifier).
//
//  Split into:
//
//    HttpError                  — the typed throw
//    normalizeError             — any throw → one shape
//                                 (a body-parser 4xx kept)
//    errorMiddleware            — the Express tail
//    formatStreamError          — log line for stream errors
//    apiCallErrorOf             — the provider error, unwrapped
//    retryAfterSeconds          — the relayed wait, clamped
//    gatewayErrorToHttpError    — gateway refusal → HttpError
//    streamErrorToAssistantText — user-facing fold
// -----------------------------------------------------------

import { APICallError } from "ai";


// The longest wait relayed from a gateway Retry-After, in
// seconds — a per-minute limit relays exactly, an "exhausted
// for the hour" is capped: a student is not told to back off
// for an hour on the strength of one header
const RETRY_AFTER_MAX_S = 600;





// -----------------------------------------------------------
// HttpError
// -----------------------------------------------------------
//
// The typed error every route throws on purpose: an HTTP
// status, a machine code, a human message, and optional
// details for the log (never for the wire) — except
// `retryAfterS`, which the chat route lifts into the
// Retry-After header.
//
// Used by:
//   - every route and service in this container
// -----------------------------------------------------------

export class HttpError extends Error {
  constructor(status, code, message, details = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}





// -----------------------------------------------------------
// normalizeError
// -----------------------------------------------------------
//
// Any thrown value → { status, code, message, details }.
// HttpError passes through; a body-parser refusal (Express's
// express.json throws http-errors: a malformed body is 400,
// one over the 2 MB limit is 413 — `expose` marks them as
// the CLIENT's fault) keeps its 4xx under our own code and
// message, because a 500 there would read on the phone as
// our outage; everything else is a generic 500 whose real
// message goes to the log, not the wire.
//
// Used by:
//   - errorMiddleware (below)
// -----------------------------------------------------------

export function normalizeError(err) {
  if (err instanceof HttpError) {
    return {
      status: err.status || 500,
      code: err.code || "ASSISTANT_ERROR",
      message: err.message || "Assistant error",
      details: err.details ?? null,
    };
  }
  const status = Number(err?.status ?? err?.statusCode);
  if (err?.expose === true && status >= 400 && status < 500) {
    const tooLarge = status === 413;
    return {
      status,
      code: tooLarge ? "PAYLOAD_TOO_LARGE" : "INVALID_BODY",
      message: tooLarge ? "Request body too large" : "Request body could not be read",
      details: err.type || err.message || null,
    };
  }
  return {
    status: 500,
    code: "INTERNAL_ERROR",
    message: "Internal assistant error",
    details: err?.message || null,
  };
}





// -----------------------------------------------------------
// errorMiddleware
// -----------------------------------------------------------
//
// Registered last on the app. Logs the normalized failure
// with its details and answers the envelope without them —
// details are for operators, the wire carries only what
// the client's failure taxonomy reads.
//
// Used by:
//   - server.js — app.use(errorMiddleware), after routes
// -----------------------------------------------------------

export function errorMiddleware(err, req, res, next) {
  const normal = normalizeError(err);
  console.error(`[${normal.status}] ${normal.code}: ${normal.message}`,
                normal.details ? JSON.stringify(normal.details).slice(0, 500) : "");
  if (res.headersSent) {
    res.end();
    return next(err);
  }
  res.status(normal.status).json({
    message: normal.message,
    error: { code: normal.code, message: normal.message },
  });
}





// -----------------------------------------------------------
// formatStreamError
// -----------------------------------------------------------
//
// The log line for a mid-stream model failure — name and
// message, never a full dump of the SDK's error object. For
// the operator's eyes only: a provider error's message IS the
// gateway's error body.
//
// Used by:
//   - routes/chat.js — streamText onError
//   - streamErrorToAssistantText (below) — non-provider errors
// -----------------------------------------------------------

export function formatStreamError(error) {
  const name = error?.name || "Error";
  const message = error?.message || String(error);
  return `${name}: ${message}`.slice(0, 300);
}





// -----------------------------------------------------------
// apiCallErrorOf
// -----------------------------------------------------------
//
// The AI SDK's APICallError behind a stream error, or null:
// the error itself, its `cause`, or a RetryError's last
// attempt. The route runs with maxRetries 0, so the bare
// form is the one seen — the wrapped forms cost nothing.
//
// Used by:
//   - gatewayErrorToHttpError, streamErrorToAssistantText (below)
// -----------------------------------------------------------

function apiCallErrorOf(error) {
  for (const candidate of [error, error?.cause, error?.lastError]) {
    if (APICallError.isInstance(candidate)) return candidate;
  }
  return null;
}





// -----------------------------------------------------------
// retryAfterSeconds
// -----------------------------------------------------------
//
//   retryAfterSeconds({ "retry-after": "30" }) → 30
//
// The gateway's Retry-After as whole seconds, or null when
// there is none worth relaying. Both grammars of the header
// (delay-seconds, HTTP-date), clamped to 0..RETRY_AFTER_MAX_S.
//
// Used by:
//   - gatewayErrorToHttpError (below)
//   - tests/gateway.errors.test.mjs
// -----------------------------------------------------------

export function retryAfterSeconds(headers) {
  const raw = headers?.["retry-after"] ?? headers?.["Retry-After"];
  if (typeof raw !== "string" || raw.trim() === "") return null;
  const value = raw.trim();
  let seconds = /^\d+(\.\d+)?$/.test(value)
    ? Number(value)
    : (Date.parse(value) - Date.now()) / 1000;
  if (!Number.isFinite(seconds)) return null;
  seconds = Math.ceil(seconds);
  return Math.min(RETRY_AFTER_MAX_S, Math.max(0, seconds));
}





// -----------------------------------------------------------
// gatewayErrorToHttpError
// -----------------------------------------------------------
//
//   gatewayErrorToHttpError(error) → HttpError
//
// A model failure that arrived BEFORE the answer started,
// translated for the phone's taxonomy:
//
//   429            → 429 GATEWAY_RATE_LIMITED (+ Retry-After
//                    when the gateway sent one)
//   401 / 403      → 503 GATEWAY_AUTH — the container's key
//                    is dead, an operator's problem, and it
//                    must not read as the student's session
//   no status      → 502 GATEWAY_UNREACHABLE — refused or
//                    dropped before any answer
//   any other      → 502 GATEWAY_ERROR — 5xx, and the 4xx
//                    that are this container's request at
//                    fault (model id, payload), never the
//                    phone's
//   a timeout      → 504 GATEWAY_TIMEOUT — the SDK's own
//                    first-chunk deadline
//   anything else  → 502 GATEWAY_ERROR
//
// The message is ours; the gateway's text stays out of the
// envelope. The upstream status rides in details for the log.
//
// Used by:
//   - routes/chat.js — awaitFirstEvent
// -----------------------------------------------------------

export function gatewayErrorToHttpError(error) {
  const api = apiCallErrorOf(error);
  if (api) {
    const status = api.statusCode;
    if (status === 429) {
      const retryAfterS = retryAfterSeconds(api.responseHeaders);
      return new HttpError(429, "GATEWAY_RATE_LIMITED", "The assistant is busy — try again shortly",
                           { upstreamStatus: status, ...(retryAfterS != null ? { retryAfterS } : {}) });
    }
    if (status === 401 || status === 403) {
      return new HttpError(503, "GATEWAY_AUTH", "The assistant's gateway refused its key — an operator must check the gateway configuration",
                           { upstreamStatus: status });
    }
    if (status == null) {
      return new HttpError(502, "GATEWAY_UNREACHABLE", "The assistant's gateway is unreachable",
                           { name: api.name });
    }
    return new HttpError(502, "GATEWAY_ERROR", "The assistant's gateway failed to answer",
                         { upstreamStatus: status });
  }
  if (error?.name === "TimeoutError" || error?.name === "AbortError") {
    return new HttpError(504, "GATEWAY_TIMEOUT", "The assistant's gateway did not answer in time",
                         { name: error.name });
  }
  return new HttpError(502, "GATEWAY_ERROR", "The assistant's gateway failed to answer",
                       { name: error?.name || "Error" });
}





// -----------------------------------------------------------
// streamErrorToAssistantText
// -----------------------------------------------------------
//
// What the USER reads when the model or a timeout dies
// mid-answer — Lithuanian first, matching the app's tongue,
// WITH the technical cause appended on purpose: students
// send screenshots when something breaks, and the shot must
// name the failure precisely enough to debug from. A
// provider error is named by class and HTTP status ONLY —
// its message is the gateway's error body, which carries
// the virtual-key identifier.
//
// Used by:
//   - routes/chat.js — toUIMessageStream onError
// -----------------------------------------------------------

export function streamErrorToAssistantText(error) {
  const api = apiCallErrorOf(error);
  const cause = api
    ? `${api.name}: ${api.statusCode != null ? `HTTP ${api.statusCode}` : "no answer from the gateway"}`
    : formatStreamError(error);
  return "Atsiprašau, įvyko klaida generuojant atsakymą. Pabandykite dar kartą. / "
       + "Sorry, something went wrong while answering. Please try again.\n\n"
       + `(${cause})`;
}
