// -----------------------------------------------------------
//  [*] Assistant — the model door
//
//  The AI SDK provider aimed at the faculty gateway
//  (ai.knf.vu.lt — Bifrost, OpenAI wire format). The key
//  rides BOTH the x-bf-vk header Bifrost demands and a
//  standard bearer, so a stock OpenAI-compatible stand-in
//  (a local test server, another gateway) accepts the same
//  container unchanged. The model id is env-only: swapping
//  what the gateway serves never touches code.
//
//  Split into:
//
//    isModelConfigured — boot/route guard
//    getModel          — the chat model instance
// -----------------------------------------------------------

import { createOpenAICompatible } from "@ai-sdk/openai-compatible";

import { AI_CHAT_MODEL, AI_GATEWAY_KEY, AI_GATEWAY_URL } from "../config.js";


// One provider for the process — headers are static, the
// SDK holds no connection state
const provider = createOpenAICompatible({
  name: "knf-gateway",
  baseURL: AI_GATEWAY_URL,
  headers: {
    "x-bf-vk": AI_GATEWAY_KEY,
    Authorization: `Bearer ${AI_GATEWAY_KEY}`,
  },
});





// -----------------------------------------------------------
// isModelConfigured
// -----------------------------------------------------------
//
// False while AI_GATEWAY_KEY is empty — the chat route then
// answers a clean 503 instead of streaming a provider error.
//
// Used by:
//   - server.js — the boot warning
//   - routes/chat.js — the pre-stream guard
// -----------------------------------------------------------

export function isModelConfigured() {
  return AI_GATEWAY_KEY.length > 0;
}





// -----------------------------------------------------------
// getModel
// -----------------------------------------------------------
//
// The chat model streamText runs on — resolved per call so
// a future per-request override (a cheaper model for guests,
// say) has one seam to grow in. The route tests use exactly
// that seam today: setModelForTests plants a scripted mock
// model so the WHOLE chat route runs deterministically with
// no gateway; the app itself never calls it.
//
// Used by:
//   - routes/chat.js
//   - tests/chat.route.test.mjs — the override
// -----------------------------------------------------------

let modelOverride = null;

export function setModelForTests(model) {
  modelOverride = model;
}

export function getModel() {
  return modelOverride ?? provider(AI_CHAT_MODEL);
}
