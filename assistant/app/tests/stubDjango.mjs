// -----------------------------------------------------------
//  [*] Tests — the stub Django
//
//  A scriptable in-process HTTP server playing every Django
//  route the container calls, so the route/tool suites can
//  run the REAL code — real Express app, real fetches, real
//  secret header — against deterministic answers. The tests
//  point DJANGO_URL here BEFORE importing any app module
//  (config.js reads the env once, at import).
//
//  `stub.script` is live state a test mutates between
//  requests; `stub.requests` records every call (method,
//  path, query, body, headers) so fire-and-forget writes —
//  persistence, telemetry — can be asserted with waitFor.
//
//  This module imports NOTHING from the app on purpose:
//  it must be importable before the env is set.
// -----------------------------------------------------------

import { createServer } from "node:http";


// The internal secret the tests run under — the stub refuses
// /internal/* without it, exactly like Django would
export const STUB_SECRET = "test-internal-secret";


export function startStubDjango() {
  const requests = [];
  const script = {
    // token → the /api/auth/me answer (absent token = 401)
    users: new Map(),
    // GET /internal/assistant/prompt
    prompt: { version: 7, text: "CORE PROMPT" },
    // thread id → { user_id } for the lookup route
    threads: new Map(),
    // POST /internal/assistant/search → { status, results }
    search: { status: 200, results: [] },
    // GET /api/schedule/filters
    roster: { groups: [], teachers: [] },
    // GET /api/schedule/events
    events: [],
    // GET /api/news
    posts: [],
  };

  const server = createServer((req, res) => {
    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString();
      const url = new URL(req.url, "http://stub");
      const body = raw ? JSON.parse(raw) : null;
      requests.push({
        method: req.method,
        path: url.pathname,
        query: Object.fromEntries(url.searchParams),
        body,
        headers: req.headers,
      });

      const json = (status, payload) => {
        res.writeHead(status, { "Content-Type": "application/json" });
        res.end(JSON.stringify(payload));
      };

      // The secret gate — an /internal/* call without it is a
      // wiring bug the suite must catch, not paper over
      if (url.pathname.startsWith("/internal/") &&
          req.headers["x-internal-secret"] !== STUB_SECRET) {
        return json(403, { error: "Forbidden" });
      }

      if (url.pathname === "/api/auth/me") {
        const token = (req.headers.authorization || "").replace(/^Bearer /, "");
        const me = script.users.get(token);
        return me ? json(200, me) : json(401, { error: "Invalid session" });
      }
      if (url.pathname === "/internal/assistant/prompt") {
        return json(200, script.prompt);
      }
      if (url.pathname === "/internal/assistant/threads/lookup") {
        const askedBy = body?.user_id || null;
        const found = (body?.ids || []).flatMap((id) => {
          const thread = script.threads.get(id);
          const mine = thread && (thread.user_id === null || thread.user_id === askedBy);
          return mine ? [{ id, title: null, preview: null, language: "lt" }] : [];
        });
        return json(200, { threads: found });
      }
      if (/^\/internal\/assistant\/threads\/[^/]+\/messages$/.test(url.pathname)) {
        return json(200, { stored: (body?.messages || []).length });
      }
      if (url.pathname === "/internal/assistant/turn-log") {
        return json(201, { logged: true });
      }
      if (url.pathname === "/internal/assistant/search") {
        return script.search.status === 200
          ? json(200, { results: script.search.results })
          : json(script.search.status, { error: "Embedding gateway unavailable" });
      }
      if (url.pathname === "/api/schedule/filters") {
        return json(200, script.roster);
      }
      if (url.pathname === "/api/schedule/events") {
        return json(200, { events: script.events });
      }
      if (url.pathname === "/api/news") {
        return json(200, { posts: script.posts });
      }
      return json(404, { error: `stub has no route for ${url.pathname}` });
    });
  });

  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      resolve({
        url: `http://127.0.0.1:${port}`,
        requests,
        script,
        close: () => new Promise((done) => server.close(done)),
      });
    });
  });
}


// -----------------------------------------------------------
// waitFor — poll until a fire-and-forget write lands
// -----------------------------------------------------------

export async function waitFor(predicate, { timeoutMs = 2000, stepMs = 20 } = {}) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const found = predicate();
    if (found) return found;
    await new Promise((resolve) => setTimeout(resolve, stepMs));
  }
  throw new Error("waitFor timed out");
}
