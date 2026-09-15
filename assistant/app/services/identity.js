// -----------------------------------------------------------
//  [*] Assistant — who is asking
//
//  Resolves the request's Authorization header against
//  Django, once per turn: no header is a GUEST (the mobile
//  transport's explicit contract — never an error), a valid
//  bearer becomes { userId }, an invalid one is the 401 the
//  engine's failure taxonomy reads as 'auth'. Answers are
//  cached briefly by token so a chatty thread screen does
//  not hammer /api/auth/me.
//
//  Split into:
//
//    resolveIdentity — header → { userId } (null = guest)
// -----------------------------------------------------------

import { publicFetch } from "./django.js";
import { HttpError } from "../middleware/errors.js";


// Seconds a validated token's answer is trusted before
// Django is asked again — short enough that a logout is
// felt, long enough to spare the per-request round trip
const CACHE_MS = 60_000;

// token → { userId, expiresAt }; entries are pruned lazily
// on their own lookups, and the map stays tiny (one row per
// active device)
const cache = new Map();





// -----------------------------------------------------------
// resolveIdentity
// -----------------------------------------------------------
//
//   resolveIdentity(req) → { userId, studyGroup, studyProgram }
//                          (all null for a guest)
//
// The one identity read every route shares. Only a
// Bearer-shaped header is even sent to Django; any other
// shape is treated as absent. Django's 401 becomes OUR 401
// (the phone shows the session problem); any other Django
// failure bubbles as-is (a 502 is not a guest). The study
// group and program ride along because the chat prompt
// grounds "my lectures" questions in them — the profile is
// public-field data the same /api/auth/me already serves.
//
// Used by:
//   - routes/chat.js and routes/threads.js — first line
// -----------------------------------------------------------

const GUEST = { userId: null, studyGroup: null, studyProgram: null };

export async function resolveIdentity(req) {
  const header = req.get("authorization") || "";
  const [scheme, token] = header.split(" ");
  if (!token || scheme.toLowerCase() !== "bearer") {
    return { ...GUEST };
  }

  const cached = cache.get(token);
  if (cached && cached.expiresAt > Date.now()) {
    return { userId: cached.userId, studyGroup: cached.studyGroup, studyProgram: cached.studyProgram };
  }
  cache.delete(token);

  let me;
  try {
    me = await publicFetch("/api/auth/me", { headers: { Authorization: `Bearer ${token}` } });
  } catch (err) {
    if (err instanceof HttpError && err.status === 401) {
      throw new HttpError(401, "SESSION_INVALID", "Session expired or invalid");
    }
    throw err;
  }

  const userId = me?.id || me?.user?.id || null;
  if (!userId) {
    throw new HttpError(401, "SESSION_INVALID", "Session expired or invalid");
  }
  const entry = {
    userId,
    studyGroup: me?.studyGroup || me?.study_group || null,
    studyProgram: me?.studyProgram || me?.study_program || null,
    expiresAt: Date.now() + CACHE_MS,
  };
  cache.set(token, entry);
  return { userId: entry.userId, studyGroup: entry.studyGroup, studyProgram: entry.studyProgram };
}
