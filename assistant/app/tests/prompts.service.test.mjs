// -----------------------------------------------------------
//  [*] Tests — the prompt memoiser under a failing store
//
//  node:test over activePrompt() against the stub Django,
//  the minute cache aged with mocked Date. The property
//  pinned: a fetch failure keeps a PREVIOUS answer for
//  another window and throws PROMPT_UNAVAILABLE when there
//  is none — never stamping the empty shape, which once
//  negative-cached "no active prompt" for a full minute
//  after a single Django hiccup.
//
//    docker exec knfapp-assistant npm test
// -----------------------------------------------------------

import assert from "node:assert/strict";
import { after, afterEach, beforeEach, mock, test } from "node:test";

import { startStubDjango, STUB_SECRET } from "./stubDjango.mjs";


// The stub must exist before any app import — config.js
// reads these once, at first import
const stub = await startStubDjango();
process.env.DJANGO_URL = stub.url;
process.env.ASSISTANT_INTERNAL_SECRET = STUB_SECRET;

const { activePrompt, resetPromptCacheForTests } = await import("../services/prompts.js");

after(() => stub.close());


const promptHits = () => stub.requests.filter((r) => r.path === "/internal/assistant/prompt").length;

// A minute and a second — past the cache window
const ageCache = () => mock.timers.tick(61_000);


beforeEach(() => {
  stub.requests.length = 0;
  resetPromptCacheForTests();
  mock.timers.enable({ apis: ["Date"], now: Date.now() });
});

afterEach(() => mock.timers.reset());


test("a failed fetch with nothing cached throws PROMPT_UNAVAILABLE and leaves the cache cold", async () => {
  stub.script.prompt = { status: 502 };
  await assert.rejects(activePrompt(), (err) => err.status === 503 && err.code === "PROMPT_UNAVAILABLE");
  assert.equal(promptHits(), 1);

  // Django is back a moment later: asked again AT ONCE — not
  // after the success window has run out
  stub.script.prompt = { version: 3, text: "CORE" };
  assert.deepEqual(await activePrompt(), { version: 3, text: "CORE" });
  assert.equal(promptHits(), 2, "the recovered store was re-asked without waiting");
});


test("a failed fetch is never PROMPT_NOT_CONFIGURED's shape — an active-but-empty answer still is", async () => {
  stub.script.prompt = { status: 502 };
  await assert.rejects(activePrompt(), (err) => err.code === "PROMPT_UNAVAILABLE" && err.code !== "PROMPT_NOT_CONFIGURED");

  // Django answering "nothing active" is a SUCCESSFUL fetch:
  // the empty shape comes back and the route names it
  stub.script.prompt = { version: null, text: "" };
  assert.deepEqual(await activePrompt(), { version: null, text: "" });
});


test("a failed fetch with a previous answer serves the stale text for another window", async () => {
  stub.script.prompt = { version: 3, text: "CORE v3" };
  assert.deepEqual(await activePrompt(), { version: 3, text: "CORE v3" });
  assert.equal(promptHits(), 1);

  ageCache();
  stub.script.prompt = { status: 502 };
  assert.deepEqual(await activePrompt(), { version: 3, text: "CORE v3" }, "stale beats failed");
  assert.equal(promptHits(), 2);
  // The failure earned a fresh window — the store is not
  // hammered while it recovers
  assert.deepEqual(await activePrompt(), { version: 3, text: "CORE v3" });
  assert.equal(promptHits(), 2, "no re-fetch inside the window");

  // And the next window picks up the recovered store's answer
  ageCache();
  stub.script.prompt = { version: 4, text: "CORE v4" };
  assert.deepEqual(await activePrompt(), { version: 4, text: "CORE v4" });
  assert.equal(promptHits(), 3);
});
