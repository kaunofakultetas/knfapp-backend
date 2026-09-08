// -----------------------------------------------------------
//  [*] auth/token — the bearer token store
//
//  The backend authenticates with an opaque bearer token
//  (returned by POST /api/auth/login, sent back on every call
//  as "Authorization: Bearer <token>"). This module is the
//  single place that token lives in the browser: one
//  localStorage key, read/written through these three
//  helpers so no other file touches storage directly.
//
//  The token survives page reloads on purpose — the panel is
//  used from trusted admin machines and the backend expires
//  sessions server-side after 30 days anyway.
// -----------------------------------------------------------


const TOKEN_KEY = 'knfappAdminToken';







// -----------------------------------------------------------
// getToken
// -----------------------------------------------------------
//
// Used by:
//   - api/client.js — the request interceptor (every call)
//   - Login.jsx — the on-mount logout of the previous session
// -----------------------------------------------------------

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}







// -----------------------------------------------------------
// setToken
// -----------------------------------------------------------
//
// Used by:
//   - Login.jsx — after a successful admin/curator login
// -----------------------------------------------------------

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}







// -----------------------------------------------------------
// clearToken
// -----------------------------------------------------------
//
// Used by:
//   - api/client.js — on a 401 (expired/killed session)
//   - AuthGuard.jsx — when the account is not admin/curator
//   - Login.jsx — the on-mount logout
//   - Account.jsx — after "log out everywhere"
// -----------------------------------------------------------

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}
