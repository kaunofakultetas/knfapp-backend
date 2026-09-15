// -----------------------------------------------------------
//  [*] Assistant — the turn rate limiter
//
//  A fixed window per identity: RATE_LIMIT_TURNS chat turns
//  per RATE_LIMIT_WINDOW_MS, keyed on the user id when
//  there is one and the client IP for guests. In-memory on
//  purpose — the container runs single-instance like the
//  Django worker, and a lost window on restart is the
//  correct failure direction (briefly generous, never
//  wrongly strict). Expired windows are swept on every
//  check so the map cannot grow past the active audience.
//
//  Split into:
//
//    checkTurnLimit — throw 429 or record the turn
// -----------------------------------------------------------

import { RATE_LIMIT_TURNS, RATE_LIMIT_WINDOW_MS } from "../config.js";
import { HttpError } from "../middleware/errors.js";


// identity key → { count, resetAt }
const windows = new Map();





// -----------------------------------------------------------
// checkTurnLimit
// -----------------------------------------------------------
//
//   checkTurnLimit("user:abc")   — records or throws 429
//
// The 429 carries Retry-After via details; the chat route
// sets the header so the engine's quota failure shows a
// real wait. Sweeping first keeps the map bounded.
//
// Used by:
//   - routes/chat.js — before any model work
// -----------------------------------------------------------

export function checkTurnLimit(key) {
  const now = Date.now();
  for (const [candidate, window] of windows) {
    if (window.resetAt <= now) windows.delete(candidate);
  }

  const window = windows.get(key);
  if (!window) {
    windows.set(key, { count: 1, resetAt: now + RATE_LIMIT_WINDOW_MS });
    return;
  }
  if (window.count >= RATE_LIMIT_TURNS) {
    const retryAfterS = Math.max(1, Math.ceil((window.resetAt - now) / 1000));
    throw new HttpError(429, "RATE_LIMITED",
                        "Per daug klausimų iš eilės — palaukite minutę. / Too many questions — wait a minute.",
                        { retryAfterS });
  }
  window.count += 1;
}
