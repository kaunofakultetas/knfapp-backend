// -----------------------------------------------------------
//  [*] api client — the app's single axios instance
//
//  Every backend call goes through this instance so the
//  cross-cutting concerns live in ONE place instead of being
//  re-implemented per call site:
//
//    - the bearer token (auth/token.js) is attached to every
//      request as "Authorization: Bearer <token>"
//    - a 401 anywhere drops the stored token and hard-
//      redirects to the login page — no page has to
//      special-case an expired or killed session
//    - any other failed MUTATION (POST/PUT/PATCH/DELETE)
//      toasts the backend's error and rethrows, so a
//      useMutation's onError has nothing left to do — the
//      modal simply stays open. Failed GETs do NOT toast
//      here — pages surface those through their query's
//      error state, and a toast per failed poll would spam.
//
//  Backend errors arrive as {"error": "<English prose>"} and
//  sometimes carry a stable "code" slug; errorText() below
//  prefers the prose (it is written for display) and falls
//  back to a translated generic sentence for responses
//  without a body (network failures, proxy pages).
//
//  Every component owns its own TanStack useQuery /
//  useMutation (there is no shared fetch hook); this
//  instance is the queryFn / mutationFn they all call. The
//  one exception is the login request, which uses raw axios
//  on purpose — a wrong password is a 401 and must not
//  trigger the redirect.
//
//  Used by:
//    - every useQuery queryFn in the app (api.get)
//    - the modals and action buttons (api.post/put/patch/
//      delete inside useMutation)
//    - AuthGuard.jsx — the /api/auth/me query
//    - Login.jsx — errorText for the error box it renders
//      itself
// -----------------------------------------------------------

import axios from 'axios';
import toast from 'react-hot-toast';
import { translate } from '@/i18n';
import { clearToken, getToken } from '@/auth/token';


export const api = axios.create();


// Attach the bearer token to every request that has one
api.interceptors.request.use((config) => {
  const token = getToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});


api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error.response?.status;

    if (status === 401) {
      // Session expired or killed — drop the dead token and
      // restart at the login page
      clearToken();
      window.location.href = `${import.meta.env.BASE_URL}login`;
      return new Promise(() => {});   // page is navigating away
    }

    const method = (error.config?.method ?? 'get').toLowerCase();
    if (method !== 'get') {
      toast.error(errorText(error), { duration: 8000 });
    }

    return Promise.reject(error);
  },
);







// -----------------------------------------------------------
// errorText
// -----------------------------------------------------------
//
// The displayable sentence for one failed request: the
// backend's "error" prose when the response carries one,
// otherwise the translated generic REQUEST_FAILED with the
// status code (a network failure, a proxy page). A 429 also
// appends how long to wait when the Retry-After header made
// it through.
//
// Used by:
//   - the interceptor above — every failed mutation
//   - Login.jsx — the error box under the login form
// -----------------------------------------------------------

export function errorText(error) {
  const data = error?.response?.data;

  if (data?.error) {
    const retryAfter = error.response?.headers?.['retry-after'];
    if (error.response?.status === 429 && retryAfter) {
      return `${data.error} (${translate('navbar.ERRORS.RETRY_AFTER', { seconds: retryAfter })})`;
    }
    return data.error;
  }

  return translate('navbar.ERRORS.REQUEST_FAILED',
                   { status_code: error?.response?.status ?? 'network' });
}


export default api;
