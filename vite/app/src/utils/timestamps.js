// -----------------------------------------------------------
//  [*] utils/timestamps — API timestamps for display
//
//  The API sends ISO-8601 strings in two shapes: most carry
//  an explicit "+00:00" offset, but some (scraper runs, chat)
//  are NAIVE UTC with no offset at all. parseTimestamp
//  normalises both to a Date; formatDateTime prints it in
//  the faculty's timezone (Europe/Vilnius) regardless of the
//  browser's own.
//
//  dateTimeColumn is the DataGrid column fragment every
//  timestamp column spreads in — sorting on the real Date,
//  printing the local string.
// -----------------------------------------------------------


// One formatter instance — building it per cell would be
// wasteful in the grids
const FORMATTER = new Intl.DateTimeFormat('lt-LT', {
  timeZone: 'Europe/Vilnius',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit',
});







// -----------------------------------------------------------
// parseTimestamp
// -----------------------------------------------------------
//
// API string → Date, or null for empty/unparsable values.
// A string without offset information is naive UTC (that is
// how the backend stores those columns), so "Z" is appended
// before parsing — otherwise the browser would read it as
// local time and shift every stamp.
//
// Used by:
//   - formatDateTime / dateTimeColumn (below)
//   - any page comparing timestamps in JS
// -----------------------------------------------------------

export function parseTimestamp(value) {
  if (!value) return null;
  const iso = /(?:Z|[+-]\d\d:?\d\d)$/.test(value) ? value : `${value}Z`;
  const date = new Date(iso);
  return isNaN(date.getTime()) ? null : date;
}







// -----------------------------------------------------------
// formatDateTime
// -----------------------------------------------------------
//
// Date (or API string) → "YYYY-MM-DD HH:mm" in
// Europe/Vilnius; empty string for nothing.
//
// Used by:
//   - dateTimeColumn (below)
//   - the detail modals that print single timestamps
// -----------------------------------------------------------

export function formatDateTime(value) {
  const date = value instanceof Date ? value : parseTimestamp(value);
  if (!date) return '';
  return FORMATTER.format(date).replace(',', '');
}







// -----------------------------------------------------------
// dateTimeColumn
// -----------------------------------------------------------
//
// Spread into a DataGrid column definition:
//   { field: 'createdAt', headerName: ..., ...dateTimeColumn }
// Sorts on the parsed Date, prints via formatDateTime.
//
// Used by:
//   - every grid with a timestamp column (users, invitations,
//     reports, news, scraper runs)
// -----------------------------------------------------------

export const dateTimeColumn = {
  type: 'dateTime',
  valueGetter: (value) => parseTimestamp(value),
  valueFormatter: (value) => (value ? formatDateTime(value) : ''),
};
