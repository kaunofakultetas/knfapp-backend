// -----------------------------------------------------------
//  [*] Tests — the Django client keeps a failure's identity
//
//  services/django.js against a raw TCP server that can break
//  an answer anywhere (this file runs in its own process —
//  config.js reads DJANGO_URL once). A 200 whose body breaks
//  off mid-read is a 502 DJANGO_UNREACHABLE, never a
//  successful null (KNF-069: read as null, a live session was
//  "expired" and a failed search an empty handbook) — and
//  through resolveIdentity it stays a 502, not the 401 that
//  sends a signed-in student to the login screen. A 2xx that
//  is not JSON is 502 DJANGO_BAD_ANSWER; an empty 2xx is null.
//  On the INTERNAL door a 401/403 is the container's own
//  secret being refused — 503 ASSISTANT_MISCONFIGURED, never
//  relayed as the phone's "sign in again" (KNF-070); on the
//  public door a 401 still describes the caller.
//
//    docker exec knfapp-assistant npm test
// -----------------------------------------------------------

import assert from "node:assert/strict";
import { createServer } from "node:net";
import { after, test } from "node:test";


// path → how the raw server answers it
const ANSWERS = {
  // Promises 400 bytes, writes 34, drops the socket
  "/api/auth/me": (socket) => {
    socket.write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 400\r\n\r\n");
    socket.write('{"id":"u-1","studyGroup":"IS-3","x');
    socket.destroy();
  },
  "/api/broken": (socket) => {
    socket.write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 400\r\n\r\n");
    socket.write('{"results":[{"id":"h1",');
    socket.destroy();
  },
  "/api/html": (socket) => {
    const body = "<html>proxy page</html>";
    socket.end(`HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: ${body.length}\r\n\r\n${body}`);
  },
  "/api/empty": (socket) => {
    socket.end("HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n");
  },
  "/api/ok": (socket) => {
    const body = '{"ok":true}';
    socket.end(`HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ${body.length}\r\n\r\n${body}`);
  },
  "/api/unauthorized": (socket) => {
    const body = '{"error":"Invalid session"}';
    socket.end(`HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: ${body.length}\r\n\r\n${body}`);
  },
  "/internal/assistant/threads/list": (socket) => {
    const body = '{"error":"Forbidden"}';
    socket.end(`HTTP/1.1 403 Forbidden\r\nContent-Type: application/json\r\nContent-Length: ${body.length}\r\n\r\n${body}`);
  },
};

const raw = createServer((socket) => {
  socket.once("data", (chunk) => {
    const path = chunk.toString().split(" ")[1]?.split("?")[0] ?? "";
    (ANSWERS[path] ?? ANSWERS["/api/empty"])(socket);
  });
});
await new Promise((resolve) => raw.listen(0, "127.0.0.1", resolve));
process.env.DJANGO_URL = `http://127.0.0.1:${raw.address().port}`;
process.env.ASSISTANT_INTERNAL_SECRET = "a-stale-secret";

const { internalFetch, publicFetch } = await import("../services/django.js");
const { resolveIdentity } = await import("../services/identity.js");

after(() => new Promise((resolve) => raw.close(resolve)));


const rejectsWith = async (promise, status, code) => {
  await assert.rejects(promise, (err) => {
    assert.equal(err.status, status);
    assert.equal(err.code, code);
    return true;
  });
};


test("KNF-069: a 200 whose body breaks off is a 502 — never a successful null", async () => {
  await rejectsWith(publicFetch("/api/broken"), 502, "DJANGO_UNREACHABLE");
});


test("KNF-069: a live session behind a broken /me read is a 502, not 'session expired'", async () => {
  const req = { get: (name) => (name.toLowerCase() === "authorization" ? "Bearer live-token" : undefined) };
  await rejectsWith(resolveIdentity(req), 502, "DJANGO_UNREACHABLE");
});


test("a 2xx that is not JSON is a 502 DJANGO_BAD_ANSWER; an EMPTY 2xx is null; JSON parses", async () => {
  await rejectsWith(publicFetch("/api/html"), 502, "DJANGO_BAD_ANSWER");
  assert.equal(await publicFetch("/api/empty"), null);
  assert.deepEqual(await publicFetch("/api/ok"), { ok: true });
});


test("KNF-070: the internal door's 403 is the container's secret — 503 ASSISTANT_MISCONFIGURED", async () => {
  await rejectsWith(internalFetch("/internal/assistant/threads/list?user_id=u-1"), 503, "ASSISTANT_MISCONFIGURED");
});


test("the public door's 401 still describes the caller — relayed as is", async () => {
  await rejectsWith(publicFetch("/api/unauthorized"), 401, "DJANGO_API_ERROR");
});
