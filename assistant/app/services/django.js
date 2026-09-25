// -----------------------------------------------------------
//  [*] Assistant — the Django clients
//
//  The container's ONLY data source. Two doors, one
//  discipline: publicFetch talks to the same /api/* the
//  mobile app uses (schedule, news, identity), internalFetch
//  talks to /internal/assistant/* wearing the compose-
//  injected shared secret. Both throw HttpError on any
//  non-OK answer — a Django 4xx keeps its status (the
//  thread routes RELAY those to the phone), a network
//  failure is a 502 — with two exceptions that keep a
//  failure's identity. A body that breaks off mid-read (a
//  worker recycled while writing) is a 502, never a
//  successful empty answer: read as `null` it once turned a
//  live session into "session expired" and a failed handbook
//  search into an empty handbook (KNF-069). And a 401/403 on
//  the INTERNAL door is the container's own plumbing (a
//  missing or rotated secret), never the student's session —
//  it becomes a loudly logged 503 ASSISTANT_MISCONFIGURED
//  instead of the phone's "sign in again" (KNF-070).
//
//  Split into:
//
//    request       — the shared fetch + error fold
//    publicFetch   — /api/* as JSON
//    internalFetch — /internal/assistant/* as JSON
// -----------------------------------------------------------

import { DJANGO_URL, INTERNAL_SECRET } from "../config.js";
import { HttpError } from "../middleware/errors.js";


// Seconds Django gets to answer — internal network, so a
// slow answer is a stuck worker, not distance
const TIMEOUT_MS = 15_000;





// -----------------------------------------------------------
// request
// -----------------------------------------------------------
//
// One fetch with the shared discipline: JSON in/out, the
// timeout, network failures folded to 502, non-OK statuses
// rethrown as HttpError carrying the upstream status and
// its message when the body offers one. The body is read as
// TEXT first, so the two ways it can go wrong stay apart: a
// read that breaks off is the transport failing (502
// DJANGO_UNREACHABLE, whatever the status line said), and a
// 2xx whose non-empty body is not JSON is a broken answer
// (502 DJANGO_BAD_ANSWER). Only a genuinely EMPTY 2xx body
// answers null. A non-OK answer may carry any body at all (an
// HTML error page) — its status speaks then. `internal` marks
// the secret-wearing door, where a 401/403 describes this
// container's credentials, not the caller.
//
// Used by:
//   - publicFetch / internalFetch (below)
// -----------------------------------------------------------

async function request(path, options, { internal = false } = {}) {
  let response;
  try {
    response = await fetch(`${DJANGO_URL}${path}`, {
      ...options,
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
  } catch (err) {
    throw new HttpError(502, "DJANGO_UNREACHABLE", "Backend unreachable",
                        { path, cause: err?.message });
  }

  let text;
  try {
    text = await response.text();
  } catch (err) {
    throw new HttpError(502, "DJANGO_UNREACHABLE", "Backend answer broke off mid-read",
                        { path, upstreamStatus: response.status, cause: err?.message });
  }
  let body = null;
  let parsed = true;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      parsed = false;
    }
  }

  if (!response.ok) {
    if (internal && (response.status === 401 || response.status === 403)) {
      console.error(`Django refused the internal secret on ${path} (${response.status}) — `
                    + "ASSISTANT_INTERNAL_SECRET is missing or differs from Django's");
      throw new HttpError(503, "ASSISTANT_MISCONFIGURED",
                          "The assistant cannot reach its backend — its internal credentials were refused",
                          { path, upstreamStatus: response.status });
    }
    throw new HttpError(response.status, "DJANGO_API_ERROR",
                        body?.error?.message || body?.message || `Backend answered ${response.status}`,
                        { path, upstreamStatus: response.status });
  }
  if (!parsed) {
    throw new HttpError(502, "DJANGO_BAD_ANSWER", "Backend answered with a malformed body",
                        { path, upstreamStatus: response.status });
  }
  return body;
}





// -----------------------------------------------------------
// publicFetch
// -----------------------------------------------------------
//
//   publicFetch("/api/schedule/events?from=...") → parsed JSON
//
// The public API door — the container calls it exactly like
// a phone would, passing a bearer only where a route wants
// one (identity).
//
// Used by:
//   - services/identity.js — GET /api/auth/me
//   - llm/tools.js — lookupSchedule, searchNews
// -----------------------------------------------------------

export function publicFetch(path, { headers = {}, ...options } = {}) {
  return request(path, {
    headers: { "Content-Type": "application/json", ...headers },
    ...options,
  });
}





// -----------------------------------------------------------
// internalFetch
// -----------------------------------------------------------
//
//   internalFetch("/internal/assistant/search", { method:
//     "POST", body: {...} }) → parsed JSON
//
// The internal door: the shared secret on every call, the
// body JSON-serialized here so callers hand plain objects. A
// 401/403 here is the SECRET being refused — 503
// ASSISTANT_MISCONFIGURED (see request).
//
// Used by:
//   - routes/threads.js — the thread CRUD relay
//   - routes/chat.js — persistence + turn log
//   - llm/tools.js — searchHandbook
// -----------------------------------------------------------

export function internalFetch(path, { body, headers = {}, ...options } = {}) {
  return request(path, {
    headers: {
      "Content-Type": "application/json",
      "X-Internal-Secret": INTERNAL_SECRET,
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    ...options,
  }, { internal: true });
}
