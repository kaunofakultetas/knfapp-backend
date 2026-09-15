// -----------------------------------------------------------
//  [*] Tests — the failure discipline
//
//  The error envelope is a CONTRACT with the mobile engine's
//  failure taxonomy ({ message, error: { code } }; 401 reads
//  as auth, 429 as quota, 502-504 as unavailable) — break
//  its shape and every phone shows the wrong error with no
//  test going red. Also pinned: a generic throw never leaks
//  its message to the wire, the mid-stream apology KEEPS its
//  technical cause (students debug by screenshot — the
//  owner's explicit call), and the turn rate limiter's 429
//  carries a real Retry-After.
//
//    docker exec knfapp-assistant npm test
// -----------------------------------------------------------

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  HttpError, errorMiddleware, formatStreamError, normalizeError, streamErrorToAssistantText,
} from "../middleware/errors.js";
import { checkTurnLimit } from "../services/ratelimit.js";
import { RATE_LIMIT_TURNS } from "../config.js";


// A minimal Express-shaped res the middleware can drive
function fakeRes() {
  const res = {
    headersSent: false,
    statusCode: null,
    body: null,
    ended: false,
    status(code) { res.statusCode = code; return res; },
    json(payload) { res.body = payload; return res; },
    end() { res.ended = true; },
  };
  return res;
}


test("an HttpError answers its own status inside the taxonomy envelope", () => {
  const res = fakeRes();
  errorMiddleware(new HttpError(429, "RATE_LIMITED", "Palaukite minutę", { retryAfterS: 30 }),
                  {}, res, () => {});
  assert.equal(res.statusCode, 429);
  assert.deepEqual(res.body, {
    message: "Palaukite minutę",
    error: { code: "RATE_LIMITED", message: "Palaukite minutę" },
  });
  assert.ok(!JSON.stringify(res.body).includes("retryAfterS"),
            "details are for the log, never the wire");
});


test("a generic throw is a 500 whose real message stays OFF the wire", () => {
  const normal = normalizeError(new Error("password=hunter2 leaked into a message"));
  assert.equal(normal.status, 500);
  assert.equal(normal.code, "INTERNAL_ERROR");
  assert.equal(normal.message, "Internal assistant error");
  assert.equal(normal.details, "password=hunter2 leaked into a message");

  const res = fakeRes();
  errorMiddleware(new Error("password=hunter2 leaked into a message"), {}, res, () => {});
  assert.ok(!JSON.stringify(res.body).includes("hunter2"));
});


test("after headers are sent the middleware only closes the stream", () => {
  const res = fakeRes();
  res.headersSent = true;
  let forwarded = false;
  errorMiddleware(new HttpError(500, "X", "y"), {}, res, () => { forwarded = true; });
  assert.equal(res.ended, true);
  assert.equal(res.body, null, "no second body on a live stream");
  assert.ok(forwarded);
});


test("the mid-stream apology names both languages AND the technical cause", () => {
  const text = streamErrorToAssistantText(new HttpError(502, "DJANGO_API_ERROR", "Backend answered 502"));
  assert.match(text, /Atsiprašau, įvyko klaida/);
  assert.match(text, /Sorry, something went wrong/);
  // The cause stays visible on purpose — screenshot debugging
  assert.match(text, /Backend answered 502/);
});


test("formatStreamError caps the log line at 300 characters", () => {
  const line = formatStreamError(new Error("x".repeat(1000)));
  assert.equal(line.length, 300);
});


test("the turn limiter admits the window then answers 429 with a real Retry-After", () => {
  const key = "user:limits-test";
  for (let turn = 0; turn < RATE_LIMIT_TURNS; turn += 1) {
    checkTurnLimit(key);
  }
  try {
    checkTurnLimit(key);
    assert.fail("expected the 429 throw");
  } catch (err) {
    assert.equal(err.status, 429);
    assert.equal(err.code, "RATE_LIMITED");
    assert.ok(err.details.retryAfterS >= 1 && err.details.retryAfterS <= 61);
  }
  // A DIFFERENT identity is untouched by the burst
  checkTurnLimit("user:limits-other");
});
