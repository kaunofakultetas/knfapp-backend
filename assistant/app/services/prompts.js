// -----------------------------------------------------------
//  [*] Assistant — the versioned system prompt
//
//  THE system prompt, database-owned (jauka's versioned
//  prompt sets, single-set): Django stores immutable
//  template versions with at most one active — edited in
//  the admin panel, never in code — and this module fetches
//  the active one for the chat route to RENDER (llm/
//  prompt.js substitutes the clock/language/profile
//  placeholders). Cached for a minute — an admin's
//  activation reaches the agent on the next cache turn, and
//  a Django hiccup serves the last known answer instead of
//  failing a chat turn. With NOTHING known yet (a cold start
//  while Django is still booting) the turn fails as 503
//  PROMPT_UNAVAILABLE and the cache stays cold, so the very
//  next turn asks again — an emptiness is never cached: that
//  read as "no active prompt" for a whole minute while the
//  row was active all along.
//
//  Split into:
//
//    activePrompt — { version, text }, cached
// -----------------------------------------------------------

import { HttpError } from "../middleware/errors.js";
import { internalFetch } from "./django.js";


// Milliseconds an answer is reused — the admin console's
// "activate" reaches live turns within this window
const CACHE_MS = 60_000;

// The last answer and when it was fetched; the EMPTY shape
// doubles as the cold-start and the no-active-row state
let cached = { at: 0, version: null, text: "" };





// -----------------------------------------------------------
// activePrompt
// -----------------------------------------------------------
//
//   activePrompt() → { version: number | null, text: string }
//
// The one reader. Failures keep the previous answer — a
// stale prompt beats a failed chat turn — and throw 503
// PROMPT_UNAVAILABLE when there is no previous answer to
// keep. An empty `text` on a SUCCESSFUL fetch is Django's
// own word that no version is active; the route turns that
// into PROMPT_NOT_CONFIGURED, a different failure.
//
// Used by:
//   - routes/chat.js — per turn
// -----------------------------------------------------------

export async function activePrompt() {
  if (Date.now() - cached.at < CACHE_MS) {
    return { version: cached.version, text: cached.text };
  }
  try {
    const answer = await internalFetch("/internal/assistant/prompt");
    cached = {
      at: Date.now(),
      version: typeof answer?.version === "number" ? answer.version : null,
      text: typeof answer?.text === "string" ? answer.text : "",
    };
  } catch (err) {
    // Only a real previous answer earns another window — the
    // stamp on an empty cache negative-cached the emptiness
    if (!cached.text) {
      console.error("Prompt fetch failed (nothing cached, next turn retries):", err?.message);
      throw new HttpError(503, "PROMPT_UNAVAILABLE",
                          "System prompt unavailable — the backend did not answer",
                          { cause: err?.message || null });
    }
    console.error("Prompt fetch failed (keeping previous):", err?.message);
    cached.at = Date.now();
  }
  return { version: cached.version, text: cached.text };
}


// The route tests script different store answers per case —
// this drops the minute cache between them. The app itself
// never calls it.
export function resetPromptCacheForTests() {
  cached = { at: 0, version: null, text: "" };
}
