// -----------------------------------------------------------
//  [*] AuthGuard — session state for the whole app
//
//  Checks the session once on app start:
//    GET /api/auth/me   (bearer token attached by api client)
//  as a TanStack query that never goes stale, and shares the
//  result through React context:
//    - the context value is authData itself — null until the
//      check finishes, then the account object from /auth/me
//      ({id, username, email, displayName, role, avatarUrl})
//    - a 401 is redirected to /login by the api client
//      itself; any other failure by this provider
//    - a valid session whose role is NOT admin/curator is
//      thrown out too — the panel is for staff only, a
//      student token has no business here even though the
//      backend would answer its read calls
//
//  No polling or refresh — a new login/logout becomes visible
//  on the next full page load.
// -----------------------------------------------------------

/* eslint-disable react-refresh/only-export-components --
   AuthProvider and its useAuth hook belong together; editing this
   file reloads the page instead of hot-swapping it, nothing more */

import { createContext, useContext, useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import api from '@/api/client';
import { clearToken } from '@/auth/token';


const AuthContext = createContext(null);


// The roles the panel admits — everyone else is bounced back
// to the login page
export const PANEL_ROLES = ['admin', 'curator'];







// -----------------------------------------------------------
// useAuth
// -----------------------------------------------------------
//
// The session data (or null while the check is running).
//
// Used by:
//   - PageWrapper.jsx — reads authData for every routed page
// -----------------------------------------------------------

export function useAuth() {
  return useContext(AuthContext);
}







// -----------------------------------------------------------
// AuthProvider
// -----------------------------------------------------------
//
// Used by:
//   - App.jsx — wraps every page except /login
// -----------------------------------------------------------

export function AuthProvider({ children }) {

  // One check per app start: never stale, never refetched on
  // focus/reconnect, never retried — a failed check is a
  // missing session, not a flaky network, and a retry would
  // only delay the redirect
  const { data: authData = null, isError } = useQuery({
    queryKey: ['auth', 'me'],
    queryFn: async () => (await api.get('/api/auth/me')).data,
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  // Not staff, or the check failed for a non-401 reason —
  // restart at the login page (the login page also drops the
  // session server-side on mount)
  const wrongRole = authData && !PANEL_ROLES.includes(authData.role);
  useEffect(() => {
    if (isError || wrongRole) {
      clearToken();
      window.location.href = `${import.meta.env.BASE_URL}login`;
    }
  }, [isError, wrongRole]);

  return (
    <AuthContext.Provider value={wrongRole ? null : authData}>
      {children}
    </AuthContext.Provider>
  );
}
