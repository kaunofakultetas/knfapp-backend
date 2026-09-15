// -----------------------------------------------------------
//  [*] Assistant — configuration
//
//  Every environment variable and tuning constant of the
//  container in one file. The gateway pair points the AI
//  SDK at the faculty's OpenAI-format gateway; DJANGO_URL +
//  INTERNAL_SECRET are the door to Django's /internal/
//  assistant routes; the limits below are the abuse
//  ceilings the chat route enforces before any model call.
// -----------------------------------------------------------


// Where Express listens (Caddy proxies /api/assistant/* here)
export const PORT = Number(process.env.PORT || 3000);

// The Django container — public API for identity + tools,
// /internal/assistant/* for threads, search and telemetry
export const DJANGO_URL = process.env.DJANGO_URL || "http://knfapp-django:8000";

// The shared secret Django's internal routes demand; empty
// means the routes refuse us and the container is useless —
// server.js warns loudly at boot
export const INTERNAL_SECRET = process.env.ASSISTANT_INTERNAL_SECRET || "";

// The faculty AI gateway (Bifrost, OpenAI wire format) — the
// key rides both x-bf-vk and a standard bearer, so any
// OpenAI-compatible stand-in accepts the same request
export const AI_GATEWAY_URL = process.env.AI_GATEWAY_URL;
export const AI_GATEWAY_KEY = process.env.AI_GATEWAY_KEY || "";
export const AI_CHAT_MODEL = process.env.AI_CHAT_MODEL || "gpt-4o";


// -----------------------------------------------------------
// Streaming ceilings
// -----------------------------------------------------------

// Whole turn / one step / one silent gap between chunks —
// jauka's proven trio, generous for tool-using answers
export const CHAT_TIMEOUT_MS = Number(process.env.CHAT_TIMEOUT_MS || 90_000);
export const CHAT_STEP_TIMEOUT_MS = Number(process.env.CHAT_STEP_TIMEOUT_MS || 30_000);
export const CHAT_CHUNK_TIMEOUT_MS = Number(process.env.CHAT_CHUNK_TIMEOUT_MS || 20_000);

// Tool-use rounds one turn may chain before the model must
// answer with text
export const MAX_STEPS = 8;


// -----------------------------------------------------------
// Abuse ceilings
// -----------------------------------------------------------

// The most UI messages one request may carry, and the most
// characters across all their text parts
export const MAX_MESSAGES = 60;
export const MAX_INPUT_CHARS = 100_000;

// The serialized size of the whole messages array — text
// parts alone are not enough (a body stuffed with tool
// payloads counts 0 text chars yet still rides to the model
// and into storage)
export const MAX_BODY_CHARS = 400_000;

// Turns per identity (user id, else client IP) per window
export const RATE_LIMIT_TURNS = 10;
export const RATE_LIMIT_WINDOW_MS = 60_000;
