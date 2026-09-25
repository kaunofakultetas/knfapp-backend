// -----------------------------------------------------------
//  [*] Tests — the gateway refusal, translated
//
//  node:test over the two folds a provider error goes
//  through: gatewayErrorToHttpError (a refusal BEFORE the
//  answer starts → the HttpError the envelope answers, with
//  the status the mobile taxonomy reads) and
//  streamErrorToAssistantText (a failure AFTER the head is
//  out → the apology inside the stream). Both must name the
//  failure without repeating the gateway's own words: its
//  error body — and therefore the SDK error's message — is
//  where the virtual-key identifier lives.
//
//    docker exec knfapp-assistant npm test
// -----------------------------------------------------------

import assert from "node:assert/strict";
import { test } from "node:test";

import { APICallError } from "ai";

import {
  HttpError, gatewayErrorToHttpError, retryAfterSeconds, streamErrorToAssistantText,
} from "../middleware/errors.js";


// The gateway's words, as the SDK hands them over: message
// and body both quote the upstream error text
const SENTINEL = "vk_SECRET";
const apiError = (statusCode, headers = {}) => new APICallError({
  message: `Insufficient credits for virtual key ${SENTINEL}`,
  url: "http://gateway.test/v1/chat/completions",
  requestBodyValues: {},
  statusCode,
  responseHeaders: headers,
  responseBody: `{"error":{"message":"virtual key ${SENTINEL} refused"}}`,
});


test("the status table: 429 stays 429 with its wait, 401/403 read as GATEWAY_AUTH 503, the rest 502", () => {
  const quota = gatewayErrorToHttpError(apiError(429, { "retry-after": "30" }));
  assert.ok(quota instanceof HttpError);
  assert.equal(quota.status, 429);
  assert.equal(quota.code, "GATEWAY_RATE_LIMITED");
  assert.equal(quota.details.retryAfterS, 30);
  assert.equal(gatewayErrorToHttpError(apiError(429)).details.retryAfterS, undefined);

  for (const status of [401, 403]) {
    const auth = gatewayErrorToHttpError(apiError(status));
    assert.equal(auth.status, 503, `gateway ${status}`);
    assert.equal(auth.code, "GATEWAY_AUTH");
    assert.equal(auth.details.upstreamStatus, status);
  }
  for (const status of [500, 502, 503, 400, 404, 413]) {
    const failed = gatewayErrorToHttpError(apiError(status));
    assert.equal(failed.status, 502, `gateway ${status}`);
    assert.equal(failed.code, "GATEWAY_ERROR");
  }
  const down = gatewayErrorToHttpError(apiError(undefined));
  assert.equal(down.status, 502);
  assert.equal(down.code, "GATEWAY_UNREACHABLE");
});


test("the envelope never carries the gateway's words — message ours, body and message kept off the wire", () => {
  for (const status of [429, 401, 500, undefined]) {
    const err = gatewayErrorToHttpError(apiError(status));
    assert.ok(!err.message.includes(SENTINEL), `status ${status}`);
    assert.ok(!JSON.stringify(err.details).includes(SENTINEL), `status ${status} details`);
  }
});


test("a timeout before the first chunk is a 504, any other throw a 502 — its message stays off the wire", () => {
  const timeout = gatewayErrorToHttpError(new DOMException("chunk timeout of 20000ms exceeded", "TimeoutError"));
  assert.equal(timeout.status, 504);
  assert.equal(timeout.code, "GATEWAY_TIMEOUT");

  const other = gatewayErrorToHttpError(new Error(`${SENTINEL} in a plain message`));
  assert.equal(other.status, 502);
  assert.equal(other.code, "GATEWAY_ERROR");
  assert.ok(!other.message.includes(SENTINEL));
});


test("the wrapped forms unwrap: an APICallError as `cause`, or as a retry's last error", () => {
  const wrapped = new Error("outer");
  wrapped.cause = apiError(429);
  assert.equal(gatewayErrorToHttpError(wrapped).status, 429);

  const retry = new Error("Failed after 3 attempts");
  retry.lastError = apiError(403);
  assert.equal(gatewayErrorToHttpError(retry).code, "GATEWAY_AUTH");
});


test("retryAfterSeconds: delay-seconds relayed, an HTTP-date converted, the ceiling applied, garbage absent", () => {
  assert.equal(retryAfterSeconds({ "retry-after": "30" }), 30);
  assert.equal(retryAfterSeconds({ "retry-after": "2.5" }), 3, "fractions round up — never retry early");
  assert.equal(retryAfterSeconds({ "retry-after": "0" }), 0);
  assert.equal(retryAfterSeconds({ "retry-after": "3600" }), 600, "an hour is capped at the ceiling");
  assert.equal(retryAfterSeconds({ "Retry-After": "12" }), 12, "either header spelling");

  const soon = new Date(Date.now() + 45_000).toUTCString();
  const fromDate = retryAfterSeconds({ "retry-after": soon });
  assert.ok(fromDate >= 44 && fromDate <= 46, `HTTP-date became a wait (${fromDate})`);
  const past = new Date(Date.now() - 45_000).toUTCString();
  assert.equal(retryAfterSeconds({ "retry-after": past }), 0, "a date already past is retry now, not never");

  assert.equal(retryAfterSeconds({ "retry-after": "soon" }), null);
  assert.equal(retryAfterSeconds({ "retry-after": "" }), null);
  assert.equal(retryAfterSeconds({}), null);
  assert.equal(retryAfterSeconds(undefined), null);
});


test("the mid-stream apology names a provider error by class and status only; a plain throw keeps its message", () => {
  const text = streamErrorToAssistantText(apiError(500));
  assert.match(text, /Atsiprašau, įvyko klaida/);
  assert.match(text, /Sorry, something went wrong/);
  assert.match(text, /\(AI_APICallError: HTTP 500\)/);
  assert.ok(!text.includes(SENTINEL), "the gateway's words never reach the stream");
  assert.match(streamErrorToAssistantText(apiError(undefined)), /AI_APICallError: no answer from the gateway/);

  // Anything that is not the gateway talking keeps its cause —
  // students debug by screenshot, the owner's explicit call
  assert.match(streamErrorToAssistantText(new Error("gateway exploded mid-answer")), /gateway exploded mid-answer/);
});
