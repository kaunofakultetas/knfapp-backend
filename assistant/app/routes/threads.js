// -----------------------------------------------------------
//  [*] Assistant — the thread routes
//
//  The public face of the stored conversations, mounted at
//  /api/assistant/threads. Every handler is the same thin
//  move (jauka's sessions proxy): resolve the caller's
//  identity, relay to Django's /internal/assistant/* door
//  with that identity attached, hand the answer back
//  unchanged. The access rule itself lives in Django, once
//  — nothing here decides who owns what.
//
//  Endpoints:
//    POST /            — create (lazily, on first send)
//    GET  /            — the signed-in list
//    POST /lookup      — guest bulk fetch by device ids
//    POST /claim       — login adopts guest threads
//    GET  /:id/messages — the transcript, to seed the runtime
//    POST /:id/feedback — thumbs on one assistant message
//    POST /:id/delete  — soft delete
// -----------------------------------------------------------

import { Router } from "express";

import { HttpError } from "../middleware/errors.js";
import { internalFetch } from "../services/django.js";
import { resolveIdentity } from "../services/identity.js";


const router = Router();

// A device registry is dozens of ids at most — mirror the
// internal cap so a huge body dies here, not in Django
const MAX_IDS = 100;

// Threads carry uuids only; anything else is refused before
// it ever reaches the internal wire as a path segment
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;


function requireUuid(value) {
  if (!UUID_RE.test(value || "")) {
    throw new HttpError(404, "THREAD_NOT_FOUND", "Thread not found");
  }
  return value.toLowerCase();
}


function requireIdList(body) {
  const ids = body?.ids;
  if (!Array.isArray(ids) || ids.length > MAX_IDS) {
    throw new HttpError(400, "INVALID_IDS", `ids must be a list of at most ${MAX_IDS}`);
  }
  return ids.map(String);
}





// POST / — mint a thread for the caller (guest or user)
router.post("/", async (req, res, next) => {
  try {
    const { userId } = await resolveIdentity(req);
    const language = (req.get("accept-language") || "lt").slice(0, 2) === "en" ? "en" : "lt";
    const created = await internalFetch("/internal/assistant/threads", {
      method: "POST",
      body: { user_id: userId, language: req.body?.language === "en" ? "en" : req.body?.language === "lt" ? "lt" : language },
    });
    res.status(201).json(created);
  } catch (err) { next(err); }
});


// GET / — the signed-in list; guests get their list via
// POST /lookup with the ids their device holds
router.get("/", async (req, res, next) => {
  try {
    const { userId } = await resolveIdentity(req);
    if (!userId) {
      throw new HttpError(401, "SIGN_IN_REQUIRED", "Guests list threads via lookup");
    }
    res.json(await internalFetch(`/internal/assistant/threads/list?user_id=${encodeURIComponent(userId)}`));
  } catch (err) { next(err); }
});


// POST /lookup — the guest thread list, by device-held ids
router.post("/lookup", async (req, res, next) => {
  try {
    const { userId } = await resolveIdentity(req);
    res.json(await internalFetch("/internal/assistant/threads/lookup", {
      method: "POST",
      body: { ids: requireIdList(req.body), user_id: userId },
    }));
  } catch (err) { next(err); }
});


// POST /claim — a signed-in device adopts its guest threads
router.post("/claim", async (req, res, next) => {
  try {
    const { userId } = await resolveIdentity(req);
    if (!userId) {
      throw new HttpError(401, "SIGN_IN_REQUIRED", "Claiming needs a session");
    }
    res.json(await internalFetch("/internal/assistant/threads/claim", {
      method: "POST",
      body: { ids: requireIdList(req.body), user_id: userId },
    }));
  } catch (err) { next(err); }
});


// GET /:id/messages — the stored transcript, oldest first
router.get("/:id/messages", async (req, res, next) => {
  try {
    const { userId } = await resolveIdentity(req);
    const threadId = requireUuid(req.params.id);
    const query = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
    res.json(await internalFetch(`/internal/assistant/threads/${threadId}/messages${query}`));
  } catch (err) { next(err); }
});


// POST /:id/feedback — the reader's thumbs on one assistant
// message: rating 1 / -1 sets, 0 clears a tapped-again thumb
router.post("/:id/feedback", async (req, res, next) => {
  try {
    const { userId } = await resolveIdentity(req);
    const threadId = requireUuid(req.params.id);
    const rating = req.body?.rating;
    if (![1, -1, 0].includes(rating) || typeof req.body?.messageId !== "string") {
      throw new HttpError(400, "INVALID_FEEDBACK", "feedback needs messageId and rating 1/-1/0");
    }
    res.json(await internalFetch(`/internal/assistant/threads/${threadId}/feedback`, {
      method: "POST",
      body: { user_id: userId, message_id: req.body.messageId, rating },
    }));
  } catch (err) { next(err); }
});


// POST /:id/delete — soft delete (cron hard-prunes later)
router.post("/:id/delete", async (req, res, next) => {
  try {
    const { userId } = await resolveIdentity(req);
    const threadId = requireUuid(req.params.id);
    res.json(await internalFetch(`/internal/assistant/threads/${threadId}/delete`, {
      method: "POST",
      body: { user_id: userId },
    }));
  } catch (err) { next(err); }
});


export default router;
