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
//  failure is a 502.
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
// its message when the body offers one.
//
// Used by:
//   - publicFetch / internalFetch (below)
// -----------------------------------------------------------

async function request(path, options) {
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

  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    throw new HttpError(response.status, "DJANGO_API_ERROR",
                        body?.error?.message || body?.message || `Backend answered ${response.status}`,
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
// body JSON-serialized here so callers hand plain objects.
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
  });
}
