// -----------------------------------------------------------
//  [*] Tests — the pure halves of the tool layer
//
//  node:test over the functions that decide WHICH group's
//  timetable a student sees and WHAT DAY "today" is — the
//  two places whose untestedness let wrong-programme
//  matches and UTC-shifted dates ship. Run with:
//
//    docker exec knfapp-assistant npm test
//
//  Every resolver case below is a real phrasing from the
//  adversarial review; the miss cases are the point — a
//  guess is worse than a question.
// -----------------------------------------------------------

import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeName, resolveRosterName, toolsPayload, vilniusToday, weekBounds } from "../llm/tools.js";


const GROUPS = ["ISKS-1", "ISKS-2", "ISKS-3", "EV-1", "EV-2", "EV-3", "AV-1", "FT-1"];


test("normalizeName folds case, diacritics, hyphens, glued digits and ordinal words", () => {
  assert.equal(normalizeName("ISKS-1"), "isks 1");
  assert.equal(normalizeName("isks1"), "isks 1");
  assert.equal(normalizeName("Trečias kursas"), "3 kursas");
  assert.equal(normalizeName("pirmam kursui"), "1 kursui");
  // "antradienis" is a weekday, never course 2
  assert.equal(normalizeName("antradienis"), "antradienis");
});


test("resolveRosterName finds every real phrasing of a real group", () => {
  assert.equal(resolveRosterName("isks 1", GROUPS), "ISKS-1");
  assert.equal(resolveRosterName("ISKS1", GROUPS), "ISKS-1");
  assert.equal(resolveRosterName("isks pirmas kursas", GROUPS), "ISKS-1");
  assert.equal(resolveRosterName("isks trecias kursas", GROUPS), "ISKS-3");
  assert.equal(resolveRosterName("ev antras kursas", GROUPS), "EV-2");
});


test("resolveRosterName MISSES instead of guessing", () => {
  // No alphabetic overlap — the old digit-only scoring made
  // this EV-3 (a different programme)
  assert.equal(resolveRosterName("informatikos 3 kursas", GROUPS), null);
  // Same letters, different year — the digit veto
  assert.equal(resolveRosterName("FT-3", GROUPS), null);
  // Nothing shared at all
  assert.equal(resolveRosterName("VVS-1", GROUPS), null);
  // A bare year is every group and therefore none
  assert.equal(resolveRosterName("1 kursas", GROUPS), null);
  assert.equal(resolveRosterName("", GROUPS), null);
});


test("containment prefers the LONGEST entry inside the query, ties are misses", () => {
  const longRoster = ["Informatikos 1 kursas", "Matematikos ir informatikos 1 kursas"];
  assert.equal(
    resolveRosterName("matematikos ir informatikos 1 kursas", longRoster),
    "Matematikos ir informatikos 1 kursas",
  );
  // Two teachers sharing a surname must be a question, not a
  // coin flip
  const teachers = ["lekt. Jonas Petraitis", "doc. Ona Petraitis"];
  assert.equal(resolveRosterName("Petraitis", teachers), null);
  assert.equal(resolveRosterName("jonas petraitis", teachers), "lekt. Jonas Petraitis");
});


test("an empty roster entry can never match", () => {
  assert.equal(resolveRosterName("isks 1", ["", "  ", "ISKS-1"]), "ISKS-1");
  assert.equal(resolveRosterName("anything", ["", null]), null);
});


test("vilniusToday answers the Kaunas calendar in ISO shape", () => {
  const { isoDate, weekday } = vilniusToday();
  assert.match(isoDate, /^\d{4}-\d{2}-\d{2}$/);
  assert.ok(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].includes(weekday));
  // The pair must agree with each other in the SAME zone
  const check = new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Vilnius", weekday: "long" })
    .format(new Date(`${isoDate}T12:00:00+03:00`));
  assert.equal(check, weekday);
});


test("weekBounds brackets any date Monday..Sunday", () => {
  assert.deepEqual(weekBounds(new Date("2026-09-15T00:00:00Z")), ["2026-09-14", "2026-09-20"]);
  assert.deepEqual(weekBounds(new Date("2026-09-14T00:00:00Z")), ["2026-09-14", "2026-09-20"]);
  assert.deepEqual(weekBounds(new Date("2026-09-20T00:00:00Z")), ["2026-09-14", "2026-09-20"]);
});


// ---------------------------------------------------------
// toolsPayload — the served half of the frozen contract
// ---------------------------------------------------------

test("toolsPayload serves the contract envelope: the three tools, name/input/output, description riding along", () => {
  const payload = toolsPayload();
  assert.deepEqual(payload.tools.map((tool) => tool.name), ["lookupSchedule", "searchNews", "searchHandbook"]);
  for (const tool of payload.tools) {
    assert.equal(typeof tool.description, "string");
    assert.equal(tool.input.type, "object");
    assert.equal(tool.output.type, "object");
    assert.ok(Array.isArray(tool.output.required), `${tool.name} output lists its required keys`);
  }
});


test("searchNews's output schema declares the optional note its execute sets on an empty answer", () => {
  const searchNews = toolsPayload().tools.find((tool) => tool.name === "searchNews");
  assert.deepEqual(searchNews.output.properties.note, { type: "string" });
  assert.ok(!searchNews.output.required.includes("note"), "note is optional");
});
