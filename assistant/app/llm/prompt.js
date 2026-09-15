// -----------------------------------------------------------
//  [*] Assistant — the system prompt, rendered
//
//  The prompt lives ONLY in the database (assistant_prompts
//  — immutable versions, one active, edited in the admin
//  panel's AI page). There is NO prompt text in code: this
//  module renders the store's active template by
//  substituting the placeholders an admin cannot know when
//  writing — the clock, the request's language and the
//  asker's profile. No active version means the chat route
//  answers 503 PROMPT_NOT_CONFIGURED rather than running
//  on anything invisible.
//
//  Placeholders a template may use:
//
//    {WEEKDAY}          — Monday … Sunday (Europe/Vilnius)
//    {TODAY}            — YYYY-MM-DD (Europe/Vilnius)
//    {LEADING_LANGUAGE} — "Lithuanian" or "English"
//    {USER_CONTEXT}     — the signed-in profile line, or the
//                         guest instructions (code-built:
//                         it holds sanitized user-typed text
//                         and conditional logic)
//
//  Split into:
//
//    buildUserContext  — the {USER_CONTEXT} value
//    buildSystemPrompt — template + turn → the prompt
// -----------------------------------------------------------

import { vilniusToday } from "./tools.js";





// -----------------------------------------------------------
// buildUserContext
// -----------------------------------------------------------
//
// The {USER_CONTEXT} value — conditional logic and
// user-typed text, which is why it stays in code: profile
// fields land inside the system prompt, so quotes and line
// breaks are stripped so a crafted study group cannot open
// its own instruction line.
//
// Used by:
//   - buildSystemPrompt (below)
// -----------------------------------------------------------

export function buildUserContext(profile = {}) {
  const clean = (value) => String(value).replace(/["\\\n\r]/g, " ").slice(0, 60).trim();
  const knownUser = [];
  if (profile.studyGroup) knownUser.push(`study group "${clean(profile.studyGroup)}"`);
  if (profile.studyProgram) knownUser.push(`study program "${clean(profile.studyProgram)}"`);
  return knownUser.length
    ? `The signed-in user's ${knownUser.join(" and ")} — when they ask about "my" lectures or timetable, use that group with lookupSchedule without asking.`
    : `The user is not signed in (or has no study group on file). When they NAME a study group in any spelling ("isks pirmas kursas", "ev-2"), pass it to lookupSchedule as-is — the tool resolves it onto the real roster and tells you if nothing matches. Ask which group they mean only when no group is named at all.`;
}





// -----------------------------------------------------------
// buildSystemPrompt
// -----------------------------------------------------------
//
//   buildSystemPrompt("lt", { studyGroup }, template)
//     → the rendered prompt for this turn
//
// Renders the store's active template by substituting the
// four placeholders. An unknown placeholder passes through
// untouched — visible in the output, so a typo in the panel
// is noticed rather than silently swallowed. The CALLER
// guards against an empty template; this function never
// invents one.
//
// Used by:
//   - routes/chat.js
// -----------------------------------------------------------

export function buildSystemPrompt(language, profile = {}, template) {
  const { isoDate: today, weekday } = vilniusToday();
  const leading = language === "en" ? "English" : "Lithuanian";

  return template
    .replaceAll("{WEEKDAY}", weekday)
    .replaceAll("{TODAY}", today)
    .replaceAll("{LEADING_LANGUAGE}", leading)
    .replaceAll("{USER_CONTEXT}", buildUserContext(profile));
}
