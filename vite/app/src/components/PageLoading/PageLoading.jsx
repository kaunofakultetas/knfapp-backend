// -----------------------------------------------------------
//  [*] PageLoading — the standard "content is loading" filler
//
//  Centered spinner (+ optional label) that takes the place
//  of a page or panel while its data loads — one look for
//  every blocking load in the app.
//
//  The label comes in already translated from the caller
//  (same pattern as QuickSearchToolbar): pass t("LOADING"),
//  or leave it off for just the spinner.
//
//  Used by:
//    - Home, FacultyInfo, Schedule — while the first
//      response is on its way
// -----------------------------------------------------------

import CircularProgress from '@mui/material/CircularProgress';

export default function PageLoading({ label }) {
  return (
    <div className="flex-1 h-full min-h-[200px] flex flex-col items-center justify-center gap-3 p-5">
      <CircularProgress />
      {label && <div className="text-muted text-sm">{label}</div>}
    </div>
  );
}
