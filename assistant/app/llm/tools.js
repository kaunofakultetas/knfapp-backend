// -----------------------------------------------------------
//  [*] Assistant — the three frozen tools
//
//  The tool contract the mobile engine pins
//  (assistantengine/src/tools/contract.ts): three names,
//  their input/output JSON schemas mirrored here VERBATIM
//  — the engine's conformance suite compares the served
//  schemas structurally, so a field changed on one side
//  turns the other side's tests red before it reaches a
//  phone. Descriptions are the one addition the contract
//  allows, and they are for the model, not the client.
//
//  Execution is thin on purpose: every tool is a fetch to
//  Django — the public API for schedule and news (the same
//  wire the phone reads), the internal pgvector search for
//  the handbook. No SQL, no caches beyond what Django
//  serves; the container stays a protocol head.
//
//  Split into:
//
//    TOOL_SCHEMAS      — the wire mirrors, served verbatim
//    toolsPayload      — GET /api/assistant/tools body
//    vilniusToday      — the Kaunas calendar date/weekday
//    normalizeName     — case/diacritics/space folding
//    resolveRosterName — free text → the roster's spelling
//    fetchRoster       — cached /api/schedule/filters read
//    weekBounds        — date → its Monday..Sunday
//    createTools       — the executable set for one turn
// -----------------------------------------------------------

import { jsonSchema, tool } from "ai";

import { internalFetch, publicFetch } from "../services/django.js";


// The most lessons / posts / entries a single tool answer
// carries — the model reads these, not a person
const MAX_LESSONS = 100;
const MAX_POSTS = 10;
const MAX_ENTRIES = 8;


// The exact schema mirrors of the client contract —
// structure frozen, descriptions free
export const TOOL_SCHEMAS = {
  lookupSchedule: {
    description: "Look up VU KNF lectures. Filter by student group and/or teacher "
      + "name (exact values as shown in the app), for one day or a whole week.",
    input: {
      type: "object",
      properties: {
        group: { type: "string", description: "Student group name, e.g. 'IS 1 kursas'" },
        teacher: { type: "string", description: "Teacher display name" },
        date: { type: "string", description: "ISO date YYYY-MM-DD; defaults to today" },
        range: { type: "string", enum: ["day", "week"] },
      },
    },
    output: {
      type: "object",
      properties: {
        lessons: {
          type: "array",
          items: {
            type: "object",
            properties: {
              title: { type: "string" },
              start: { type: "string" },
              end: { type: "string" },
              room: { type: "string" },
              teacher: { type: "string" },
              group: { type: "string" },
              kind: { type: "string" },
            },
            required: ["title", "start", "end"],
          },
        },
        source: { type: "string", enum: ["live", "cache"] },
        note: { type: "string" },
      },
      required: ["lessons", "source"],
    },
  },

  searchNews: {
    description: "Search faculty news and announcements by keyword; optionally "
      + "filter by source. Returns the newest matches first.",
    input: {
      type: "object",
      properties: {
        query: { type: "string", description: "Keyword to match in title or body" },
        source: { type: "string" },
        limit: { type: "integer" },
      },
    },
    output: {
      type: "object",
      properties: {
        posts: {
          type: "array",
          items: {
            type: "object",
            properties: {
              id: { type: "string" },
              title: { type: "string" },
              summary: { type: "string" },
              date: { type: "string" },
              source: { type: "string" },
              url: { type: "string" },
            },
            required: ["id", "title", "date", "source"],
          },
        },
        // Set by execute on an empty answer (see below) — declared
        // so the served contract matches what the model receives
        note: { type: "string" },
      },
      required: ["posts"],
    },
  },

  searchHandbook: {
    description: "Semantic search over the faculty handbook: contacts, opening "
      + "hours, study programs, FAQ and practical how-to answers. Ask in the "
      + "user's own words.",
    input: {
      type: "object",
      properties: {
        query: { type: "string", description: "The question, in natural language" },
        limit: { type: "integer" },
      },
      required: ["query"],
    },
    output: {
      type: "object",
      properties: {
        entries: {
          type: "array",
          items: {
            type: "object",
            properties: {
              id: { type: "string" },
              title: { type: "string" },
              excerpt: { type: "string" },
              section: { type: "string" },
              language: { type: "string", enum: ["lt", "en"] },
            },
            required: ["id", "title", "excerpt", "language"],
          },
        },
      },
      required: ["entries"],
    },
  },
};





// -----------------------------------------------------------
// toolsPayload
// -----------------------------------------------------------
//
// The GET /api/assistant/tools body, exactly the envelope
// fetchAssistantTools expects: { tools: [{ name, input,
// output }] } (description rides along — the client guard
// ignores extra keys, the conformance normalizer strips
// documentation keys before comparing).
//
// Used by:
//   - routes/tools.js
// -----------------------------------------------------------

export function toolsPayload() {
  return {
    tools: Object.entries(TOOL_SCHEMAS).map(([name, schema]) => ({
      name,
      description: schema.description,
      input: schema.input,
      output: schema.output,
    })),
  };
}





// -----------------------------------------------------------
// vilniusToday
// -----------------------------------------------------------
//
// The date and weekday IN KAUNAS — every "šiandien/rytoj"
// computation starts here. The container's clock is UTC and
// Lithuania is UTC+2/+3, so toISOString() is yesterday every
// evening; sv-SE formatting gives ISO shape in any zone.
//
// Used by:
//   - createTools (below) — lookupSchedule's default date
//   - llm/prompt.js — the "Today is …" line
// -----------------------------------------------------------

export function vilniusToday() {
  const now = new Date();
  const isoDate = new Intl.DateTimeFormat("sv-SE", { timeZone: "Europe/Vilnius" }).format(now);
  const weekday = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Vilnius", weekday: "long" }).format(now);
  return { isoDate, weekday };
}







// -----------------------------------------------------------
// normalizeName / resolveRosterName
// -----------------------------------------------------------
//
// The schedule API filters by EXACT roster strings, but the
// model writes what the student wrote — "informatikos 3
// kursas", missing diacritics, stray case. Resolution is
// implementation-side (the tool contract's inputs stay free
// strings): normalize both sides (lowercase, diacritics
// stripped, spaces collapsed), then take an exact
// normalized hit, else a containment hit either way, else
// the entry sharing the most word tokens (ties break to the
// shorter name; sharing nothing is a miss). A miss returns
// null and the tool answers with nearby options instead of
// an empty timetable presented as truth.
//
// Used by:
//   - createTools (below) — lookupSchedule
// -----------------------------------------------------------

// Lithuanian ordinal words → the course digit the roster
// speaks; matched as WHOLE tokens so "antradienis" stays a
// weekday, never course 2
const ORDINAL_RE = /^(pirm|antr|trec|ketvirt|penkt|sest)(i?as|i?a|i?o|i?am|i?ai|i?ais|i?ame|i?os|oji|asis)?$/;
const ORDINAL_DIGIT = { pirm: "1", antr: "2", trec: "3", ketvirt: "4", penkt: "5", sest: "6" };

export function normalizeName(value) {
  const folded = String(value || "")
    .toLowerCase()
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    // Hyphens and glued letter-digit seams fold to spaces, so
    // "ISKS-1", "isks 1" and "isks1" all normalise identically
    .replace(/-/g, " ")
    .replace(/(\p{L})(\p{N})/gu, "$1 $2")
    .replace(/(\p{N})(\p{L})/gu, "$1 $2")
    .replace(/\s+/g, " ")
    .trim();
  return folded
    .split(" ")
    .map((token) => {
      const ordinal = ORDINAL_RE.exec(token);
      return ordinal ? ORDINAL_DIGIT[ordinal[1]] : token;
    })
    .join(" ");
}

export function resolveRosterName(input, roster) {
  const wanted = normalizeName(input);
  if (!wanted) return null;

  const entries = roster
    .filter((name) => typeof name === "string" && name.trim())
    .map((name) => ({ name, normal: normalizeName(name) }))
    .filter((entry) => entry.normal);
  const exact = entries.find((entry) => entry.normal === wanted);
  if (exact) return exact.name;

  // Containment: an entry inside the query prefers the LONGEST
  // entry ("matematikos ir informatikos 1 kursas" must not fold
  // to "informatikos 1 kursas"); the query inside an entry
  // prefers the shortest entry as before
  const entryInWanted = entries.filter((entry) => wanted.includes(entry.normal));
  if (entryInWanted.length > 0) {
    entryInWanted.sort((a, b) => b.normal.length - a.normal.length);
    return entryInWanted[0].name;
  }
  // The query inside an entry counts only when it is
  // UNAMBIGUOUS — a bare surname matching two teachers (or a
  // bare year matching every long group name) is a question
  // for the student, never a pick
  const wantedInEntry = entries.filter((entry) => entry.normal.includes(wanted));
  if (wantedInEntry.length === 1) {
    return wantedInEntry[0].name;
  }
  if (wantedInEntry.length > 1) {
    return null;
  }

  // Token overlap — but a course DIGIT alone is never a match:
  // every roster name carries one, so digits-only overlap is
  // how "informatikos 3 kursas" used to become EV-3. At least
  // one ALPHABETIC token must be shared, ties between distinct
  // entries are a MISS (the tool then asks, never guesses)
  const wantedTokens = new Set(wanted.split(" "));
  const wantedDigits = new Set([...wantedTokens].filter((token) => /^\p{N}+$/u.test(token)));
  let best = null;
  let bestScore = 0;
  let tied = false;
  for (const entry of entries) {
    const entryTokens = new Set(entry.normal.split(" "));
    // A year-digit CONFLICT is a veto: "FT 3" must never fold
    // to FT-1 just because the letters agree
    const entryDigits = [...entryTokens].filter((token) => /^\p{N}+$/u.test(token));
    if (wantedDigits.size > 0 && entryDigits.length > 0
        && !entryDigits.some((digit) => wantedDigits.has(digit))) {
      continue;
    }
    let score = 0;
    let alphaShared = false;
    for (const token of entryTokens) {
      if (!wantedTokens.has(token)) continue;
      score += 1;
      if (/\p{L}/u.test(token)) alphaShared = true;
    }
    if (!alphaShared) continue;
    if (score > bestScore) {
      best = entry;
      bestScore = score;
      tied = false;
    } else if (score === bestScore && score > 0 && entry.normal !== best?.normal) {
      tied = true;
    }
  }
  return best && !tied ? best.name : null;
}





// -----------------------------------------------------------
// fetchRoster
// -----------------------------------------------------------
//
// The schedule filter roster (group names + teacher names),
// cached briefly — it changes on the scraper's clock, not
// per question. A failed fetch answers empty lists: the
// tool then passes names through unresolved rather than
// failing the whole lookup over the roster side-channel.
//
// Used by:
//   - createTools (below) — lookupSchedule
// -----------------------------------------------------------

// Minutes the roster answer is reused before Django is asked
// again
const ROSTER_CACHE_MS = 5 * 60_000;

let rosterCache = { at: 0, groups: [], teachers: [], stale: true };

async function fetchRoster() {
  if (Date.now() - rosterCache.at < ROSTER_CACHE_MS) return rosterCache;
  try {
    const answer = await publicFetch("/api/schedule/filters");
    rosterCache = {
      at: Date.now(),
      groups: Array.isArray(answer?.groups) ? answer.groups : [],
      teachers: Array.isArray(answer?.teachers) ? answer.teachers : [],
      stale: false,
    };
  } catch {
    // KEEP the previous roster — a one-blip failure must not
    // turn resolution off for the whole cache window; retry
    // soon, and mark staleness so the tool can say so
    rosterCache = { ...rosterCache, at: Date.now() - ROSTER_CACHE_MS + 15_000, stale: true };
  }
  return rosterCache;
}





// -----------------------------------------------------------
// weekBounds
// -----------------------------------------------------------
//
//   weekBounds(new Date("2026-09-17")) → ["2026-09-14",
//                                         "2026-09-20"]
//
// The Monday and Sunday around a date, as ISO strings —
// computed in UTC off the ISO date alone, so the container's
// clock zone never shifts a school week.
//
// Used by:
//   - createTools (below) — lookupSchedule's week range
// -----------------------------------------------------------

export function weekBounds(date) {
  const monday = new Date(date);
  const weekday = (monday.getUTCDay() + 6) % 7;
  monday.setUTCDate(monday.getUTCDate() - weekday);
  const sunday = new Date(monday);
  sunday.setUTCDate(monday.getUTCDate() + 6);
  return [monday.toISOString().slice(0, 10), sunday.toISOString().slice(0, 10)];
}





// -----------------------------------------------------------
// createTools
// -----------------------------------------------------------
//
//   createTools({ language, record }) → { lookupSchedule, ... }
//
// The executable set for one turn, language closed over so
// searchHandbook boosts the asker's tongue. Failures throw
// — the AI SDK folds a thrown execute into a tool-error
// part, the kit shows its "failed" card, and the model gets
// to apologize or retry; nothing here ever kills the
// stream. `record`, when given, receives one {name, ms, ok}
// per call as it settles — the telemetry row's tool_calls,
// measured HERE because only the execute knows whether it
// really succeeded and how long it took.
//
// Used by:
//   - routes/chat.js — per turn
// -----------------------------------------------------------

// Wraps an execute so its outcome and duration reach the
// recorder whether it returns or throws — the throw always
// continues on to the AI SDK's tool-error handling
function timed(name, record, execute) {
  if (!record) return execute;
  return async (input) => {
    const started = Date.now();
    try {
      const result = await execute(input);
      record({ name, ms: Date.now() - started, ok: true });
      return result;
    } catch (err) {
      record({ name, ms: Date.now() - started, ok: false });
      throw err;
    }
  };
}

export function createTools({ language, record = null }) {
  const lookupSchedule = tool({
    description: TOOL_SCHEMAS.lookupSchedule.description,
    inputSchema: jsonSchema(TOOL_SCHEMAS.lookupSchedule.input),
    execute: timed("lookupSchedule", record, async ({ group, teacher, date, range }) => {
      const day = /^\d{4}-\d{2}-\d{2}$/.test(date || "") ? date : vilniusToday().isoDate;
      const [from, to] = range === "week" ? weekBounds(new Date(`${day}T00:00:00Z`)) : [day, day];
      const notes = [];

      // The model writes what the student wrote — resolve it
      // onto the roster's exact strings before filtering, and
      // answer with options instead of a false empty timetable
      // when nothing on the roster is close
      const roster = await fetchRoster();
      if (roster.stale) {
        notes.push("The group/teacher roster is momentarily unavailable, so names were used exactly as typed — an empty result may just mean a spelling mismatch.");
      }
      let resolvedGroup = group;
      if (group && roster.groups.length) {
        resolvedGroup = resolveRosterName(group, roster.groups);
        if (!resolvedGroup) {
          return {
            lessons: [],
            source: "live",
            note: `No study group matches "${group}". Known groups include: ${roster.groups.slice(0, 30).join(", ")}. Ask the user which one they mean.`,
          };
        }
      }
      let resolvedTeacher = teacher;
      if (teacher && roster.teachers.length) {
        resolvedTeacher = resolveRosterName(teacher, roster.teachers);
        if (!resolvedTeacher) {
          return {
            lessons: [],
            source: "live",
            note: `No teacher matches "${teacher}" on the roster. Ask the user to check the surname's spelling.`,
          };
        }
      }

      const params = new URLSearchParams({ from, to });
      if (resolvedGroup) params.set("group", resolvedGroup);
      if (resolvedTeacher) params.set("teacher", resolvedTeacher);
      const answer = await publicFetch(`/api/schedule/events?${params}`);

      const events = Array.isArray(answer?.events) ? answer.events : [];
      const lessons = events.slice(0, MAX_LESSONS).map((event) => ({
        title: event.title,
        start: `${event.date} ${event.timeStart}`,
        end: `${event.date} ${event.timeEnd}`,
        room: event.room || "",
        teacher: event.teacher || "",
        group: event.group || "",
        kind: event.lectureType || "",
      }));

      // Notes ACCUMULATE — a truncated whole-faculty answer must
      // say both things, and an empty answer must say what the
      // data does not contain instead of implying a free day
      if (!group && !teacher) {
        notes.push("No group or teacher filter was given — this is the whole faculty's timetable for the range.");
      }
      if (events.length > MAX_LESSONS) {
        notes.push(`Truncated to the first ${MAX_LESSONS} of ${events.length} lessons (ordered by date) — later days of the range are missing here.`);
      }
      if ((resolvedGroup && resolvedGroup !== group) || (resolvedTeacher && resolvedTeacher !== teacher)) {
        // The model should answer with the roster's spelling,
        // not silently pretend the student's guess was exact
        notes.push(`Filtered by ${[resolvedGroup && `group "${resolvedGroup}"`, resolvedTeacher && `teacher "${resolvedTeacher}"`].filter(Boolean).join(" and ")} (closest roster match to the request).`);
      }
      if (lessons.length === 0) {
        notes.push(`No scheduled lectures found for ${from === to ? from : `${from}..${to}`}. NOTE: this data covers regular scheduled lectures only — retake exams, individual consultations and all-day events are NOT included, and dates outside the scraped semester window return empty. Do not conclude the student is free; say what the data covers and point to VU IS for retakes/exams.`);
      }
      const result = { lessons, source: "live" };
      if (notes.length) result.note = notes.join(" ");
      return result;
    }),
  });

  const searchNews = tool({
    description: TOOL_SCHEMAS.searchNews.description,
    inputSchema: jsonSchema(TOOL_SCHEMAS.searchNews.input),
    execute: timed("searchNews", record, async ({ query, source, limit }) => {
      // The FEED is engagement-ranked; the tool's contract is
      // newest matches first — so filter SERVER-side (the q
      // param stems Lithuanian endings) and re-sort by date
      const params = new URLSearchParams({ per_page: "50" });
      if (query && query.trim()) params.set("q", query.trim());
      const answer = await publicFetch(`/api/news?${params}`);
      const rows = Array.isArray(answer?.posts) ? answer.posts : [];

      const matches = rows
        .filter((post) => !source || post.source === source)
        .sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));

      const count = Math.max(1, Math.min(Number(limit) || 5, MAX_POSTS));
      const result = {
        posts: matches.slice(0, count).map((post) => {
          const entry = {
            id: String(post.id),
            title: post.title || "",
            date: String(post.date || ""),
            source: post.source || "app",
          };
          if (post.summary) entry.summary = post.summary;
          if (post.sourceUrl) entry.url = post.sourceUrl;
          return entry;
        }),
      };
      if (result.posts.length === 0 && (query || source)) {
        result.note = "No posts matched the filter — the archive may still hold older items; suggest the news tab for browsing.";
      }
      return result;
    }),
  });

  const searchHandbook = tool({
    description: TOOL_SCHEMAS.searchHandbook.description,
    inputSchema: jsonSchema(TOOL_SCHEMAS.searchHandbook.input),
    execute: timed("searchHandbook", record, async ({ query, limit }) => {
      const answer = await internalFetch("/internal/assistant/search", {
        method: "POST",
        body: { query, limit: Math.max(1, Math.min(Number(limit) || 5, MAX_ENTRIES)), language },
      });
      return {
        entries: (answer?.results || []).map((row) => {
          const entry = {
            id: row.id,
            title: row.title,
            excerpt: row.excerpt,
            language: row.language === "en" ? "en" : "lt",
          };
          if (row.section) entry.section = row.section;
          return entry;
        }),
      };
    }),
  });

  return { lookupSchedule, searchNews, searchHandbook };
}
