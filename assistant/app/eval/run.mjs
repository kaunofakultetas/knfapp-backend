// -----------------------------------------------------------
//  [*] Assistant — the golden-question eval
//
//  Twenty real student questions against the LIVE wire:
//  each is POSTed to /api/assistant/chat exactly as the
//  phone would send it, the UI message stream is parsed,
//  and the run reports per question whether the EXPECTED
//  TOOL was called (null = the model must answer or refuse
//  without tools) and whether the answer text matches the
//  mustMatch regexes (any one alternative per entry
//  suffices). This is the regression net for every prompt,
//  chunking or threshold change — and the honest way to
//  tune search.py's MAX_DISTANCE once real embeddings
//  exist.
//
//    docker exec knfapp-assistant node eval/run.mjs
//    docker exec knfapp-assistant node eval/run.mjs --only=3
//
//  Needs AI_GATEWAY_KEY set (it exercises the real model);
//  without it the chat answers 503 and the run reports
//  that instead of failing cryptically. Questions live in
//  eval/questions.json — add one whenever a thumbs-down
//  teaches something.
// -----------------------------------------------------------

import { readFileSync } from "node:fs";

const BASE = process.env.EVAL_BASE_URL || "http://localhost:3000";
const only = process.argv.find((arg) => arg.startsWith("--only="))?.slice(7);

const questions = JSON.parse(readFileSync(new URL("./questions.json", import.meta.url), "utf8"));





// -----------------------------------------------------------
// askOnce
// -----------------------------------------------------------
//
// One stateless chat turn: the SSE stream parsed into the
// answer text, the tool names that ran, and token usage
// when the finish frame carries it.
//
// Used by:
//   - main (below)
// -----------------------------------------------------------

async function askOnce(question, language) {
  let response;
  for (let attempt = 0; ; attempt += 1) {
    response = await fetch(`${BASE}/api/assistant/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept-Language": language },
      body: JSON.stringify({
        id: null,
        messages: [{ id: "e1", role: "user", parts: [{ type: "text", text: question }] }],
        trigger: "submit-message",
      }),
    });
    // The container's own rate limiter answers 429 with
    // Retry-After — the eval waits it out like a polite
    // client instead of failing the question
    if (response.status !== 429 || attempt >= 3) break;
    const waitS = Math.min(90, Number(response.headers.get("retry-after")) || 30);
    console.log(`       (rate limited — waiting ${waitS}s)`);
    await new Promise((resolve) => setTimeout(resolve, waitS * 1000));
  }
  if (!response.ok) {
    const body = await response.text();
    return { error: `HTTP ${response.status}: ${body.slice(0, 120)}` };
  }

  let text = "";
  const tools = new Set();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let carry = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    carry += decoder.decode(value, { stream: true });
    const lines = carry.split("\n");
    carry = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.startsWith("data: ")) continue;
      const payload = line.slice(6).trim();
      if (payload === "[DONE]") continue;
      let frame;
      try { frame = JSON.parse(payload); } catch { continue; }
      if (frame.type === "text-delta" && typeof frame.delta === "string") text += frame.delta;
      if (typeof frame.type === "string" && frame.type.startsWith("tool-")) {
        tools.add(frame.type.replace(/^tool-/, "").replace(/-(input|output).*$/, ""));
      }
      if (frame.type === "tool-input-start" && frame.toolName) tools.add(frame.toolName);
    }
  }
  return { text, tools: [...tools] };
}





// -----------------------------------------------------------
// main
// -----------------------------------------------------------

let passed = 0;
let failed = 0;
const rows = [];

for (const [index, entry] of questions.entries()) {
  if (only !== undefined && String(index) !== only) continue;

  let verdicts = [];
  let answer;
  try {
    answer = await askOnce(entry.q, entry.lang);
  } catch (err) {
    answer = { error: err?.message || String(err) };
  }

  if (answer.error) {
    verdicts.push(`ERROR ${answer.error}`);
  } else {
    if (entry.expectTool) {
      const hit = answer.tools.some((name) => name.includes(entry.expectTool));
      if (!hit) verdicts.push(`expected tool ${entry.expectTool}, ran [${answer.tools.join(", ") || "none"}]`);
    }
    for (const pattern of entry.mustMatch) {
      if (pattern && !new RegExp(pattern, "iu").test(answer.text)) {
        verdicts.push(`answer misses /${pattern}/`);
      }
    }
  }

  const ok = verdicts.length === 0;
  if (ok) passed += 1; else failed += 1;
  rows.push({ index, q: entry.q, ok, verdicts, tools: answer.tools, chars: answer.text?.length });
  console.log(`${ok ? "PASS" : "FAIL"} [${index}] ${entry.q}`);
  for (const verdict of verdicts) console.log(`       ${verdict}`);
}

console.log(`\n${passed} passed, ${failed} failed of ${passed + failed}`);
process.exit(failed > 0 ? 1 : 0);
