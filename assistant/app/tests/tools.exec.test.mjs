// -----------------------------------------------------------
//  [*] Tests — the tool execute bodies
//
//  The three tools' EXECUTION against the stub Django: what
//  each one actually sends upstream and, above all, the
//  NOTES contract — the truthful-empty-timetable disclaimer
//  (retake exams are not in the data), the whole-faculty
//  and truncation warnings, the resolved-spelling note, the
//  unknown-group options answer. These rules were all P1
//  fixes guarded only by the live eval until now.
//
//  The roster cache is module-level and warm after the
//  first fetch — every case here scripts ONE roster and
//  works within it, which is also how production behaves.
//
//    docker exec knfapp-assistant npm test
// -----------------------------------------------------------

import assert from "node:assert/strict";
import { after, beforeEach, test } from "node:test";

import { startStubDjango, STUB_SECRET } from "./stubDjango.mjs";


const stub = await startStubDjango();
process.env.DJANGO_URL = stub.url;
process.env.ASSISTANT_INTERNAL_SECRET = STUB_SECRET;

const { createTools, vilniusToday } = await import("../llm/tools.js");

after(() => stub.close());


stub.script.roster = {
  groups: ["ISKS-1", "ISKS-2", "EV-1", "Informatikos 3 kursas"],
  teachers: ["doc. Jonas Petraitis"],
};

const makeEvent = (index = 0) => ({
  title: `Paskaita ${index}`, date: "2026-09-17", timeStart: "10:00", timeEnd: "11:30",
  room: "404", teacher: "doc. Jonas Petraitis", group: "ISKS-1", lectureType: "Paskaita",
});

let recorded;
let tools;

beforeEach(() => {
  stub.requests.length = 0;
  stub.script.events = [];
  stub.script.posts = [];
  stub.script.search = { status: 200, results: [] };
  recorded = [];
  tools = createTools({ language: "lt", record: (row) => recorded.push(row) });
});

const eventsRequest = () => stub.requests.find((r) => r.path === "/api/schedule/events");


// ---------------------------------------------------------
// lookupSchedule
// ---------------------------------------------------------

test("lookupSchedule resolves the student's phrasing onto the roster and says so", async () => {
  stub.script.events = [makeEvent()];
  const result = await tools.lookupSchedule.execute({ group: "isks pirmas kursas" });

  assert.equal(eventsRequest().query.group, "ISKS-1", "the ROSTER spelling went upstream");
  assert.equal(result.lessons.length, 1);
  assert.equal(result.lessons[0].start, "2026-09-17 10:00");
  assert.match(result.note, /ISKS-1.*closest roster match/s, "the answer admits the spelling was resolved");
  assert.deepEqual(recorded.map(({ name, ok }) => ({ name, ok })),
                   [{ name: "lookupSchedule", ok: true }]);
});


test("an unresolvable group answers OPTIONS, not a false empty timetable — and never queries", async () => {
  const result = await tools.lookupSchedule.execute({ group: "FT-3" });
  assert.equal(result.lessons.length, 0);
  assert.match(result.note, /No study group matches "FT-3"/);
  assert.match(result.note, /ISKS-1/, "the note lists real options");
  assert.match(result.note, /Ask the user/);
  assert.equal(eventsRequest(), undefined, "no schedule query for a group that resolved to nothing");
});


test("an empty timetable explains what the data does NOT contain (retakes, consultations)", async () => {
  const result = await tools.lookupSchedule.execute({ group: "ISKS-1", date: "2026-09-19" });
  assert.equal(result.lessons.length, 0);
  assert.match(result.note, /retake exams.*NOT included/s);
  assert.match(result.note, /Do not conclude the student is free/);
});


test("a filterless answer is flagged whole-faculty, and truncation is admitted alongside", async () => {
  stub.script.events = Array.from({ length: 105 }, (_, index) => makeEvent(index));
  const result = await tools.lookupSchedule.execute({ range: "week", date: "2026-09-17" });

  assert.equal(result.lessons.length, 100, "capped at MAX_LESSONS");
  assert.match(result.note, /whole faculty's timetable/);
  assert.match(result.note, /first 100 of 105/, "the truncation is named, not hidden");
  // The week range really brackets the date's Monday..Sunday
  assert.equal(eventsRequest().query.from, "2026-09-14");
  assert.equal(eventsRequest().query.to, "2026-09-20");
});


test("a garbage date falls back to TODAY in Kaunas, not UTC", async () => {
  stub.script.events = [];
  await tools.lookupSchedule.execute({ group: "ISKS-1", date: "rytoj" });
  assert.equal(eventsRequest().query.from, vilniusToday().isoDate);
});


// ---------------------------------------------------------
// searchNews
// ---------------------------------------------------------

test("searchNews filters SERVER-side and re-sorts newest first", async () => {
  stub.script.posts = [
    { id: 1, title: "Sena", date: "2026-09-01", source: "faculty", summary: "s" },
    { id: 2, title: "Nauja", date: "2026-09-15", source: "faculty", sourceUrl: "https://knf.vu.lt/n" },
    { id: 3, title: "Vidurinė", date: "2026-09-10", source: "faculty" },
  ];
  const result = await tools.searchNews.execute({ query: "stipendija", limit: 2 });

  const news = stub.requests.find((r) => r.path === "/api/news");
  assert.equal(news.query.q, "stipendija", "the query went to the server's q= filter");
  assert.deepEqual(result.posts.map((post) => post.title), ["Nauja", "Vidurinė"],
                   "date-sorted, limit-clamped");
  assert.equal(result.posts[0].url, "https://knf.vu.lt/n");
  assert.equal(recorded[0].name, "searchNews");
});


test("an empty news answer carries its note instead of a bare []", async () => {
  const result = await tools.searchNews.execute({ query: "niekas" });
  assert.equal(result.posts.length, 0);
  assert.match(result.note, /No posts matched/);
});


// ---------------------------------------------------------
// searchHandbook
// ---------------------------------------------------------

test("searchHandbook passes the asker's language, clamps the limit, and maps entries", async () => {
  stub.script.search = { status: 200, results: [
    { id: "c1", title: "Biblioteka", excerpt: "8–20", section: "hours", language: "lt" },
    { id: "c2", title: "Wi-Fi", excerpt: "eduroam", language: "xx" },
  ]};
  const result = await tools.searchHandbook.execute({ query: "darbo laikas", limit: 999 });

  const search = stub.requests.find((r) => r.path === "/internal/assistant/search");
  assert.equal(search.body.language, "lt");
  assert.equal(search.body.limit, 8, "clamped to MAX_ENTRIES");
  assert.equal(result.entries[0].section, "hours");
  assert.equal(result.entries[1].section, undefined, "absent section stays absent");
  assert.equal(result.entries[1].language, "lt", "unknown language folds to lt");
});


test("a Django failure throws (the SDK's tool-error path) and records ok:false with ms", async () => {
  stub.script.search = { status: 502 };
  await assert.rejects(() => tools.searchHandbook.execute({ query: "x" }));
  assert.equal(recorded.length, 1);
  assert.equal(recorded[0].ok, false);
  assert.equal(typeof recorded[0].ms, "number");
});
