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
//  failing a chat turn (empty on a cold start: the code's
//  bootstrap template steps in).
//
//  Split into:
//
//    activePrompt — { version, text }, cached
// -----------------------------------------------------------

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
// stale prompt beats a failed chat turn.
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
