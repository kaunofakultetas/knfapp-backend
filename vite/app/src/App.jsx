// -----------------------------------------------------------
//  [*] App — global providers and page keep-alive
//
//  The root layout route: every page in router.jsx renders
//  through its outlet. Wraps pages in the auth and theme/i18n
//  providers and caches up to 15 visited pages with KeepAlive
//  (one cache entry per pathname), so returning to a page
//  restores its state instead of remounting it.
//
//  Special cases:
//    - /login renders the outlet inside the IntlProvider ONLY:
//      no theme (the page styles itself), no KeepAlive cache,
//      and no auth check — a failed check hard-redirects to
//      /login, so running it there would loop. The i18n
//      provider is what lets the page follow the language
//      cookie and offer the LT/EN toggle.
// -----------------------------------------------------------

import { useMemo } from 'react';
import { useLocation, useOutlet } from 'react-router-dom';
import { KeepAlive } from 'keepalive-for-react';
import Providers from '@/providers';
import { AuthProvider } from '@/AuthGuard';
import { IntlProvider } from '@/i18n';







// -----------------------------------------------------------
// App (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — element of the "/" layout route
// -----------------------------------------------------------

export default function App() {

  const { pathname } = useLocation();
  const outlet = useOutlet();

  // One KeepAlive cache entry per pathname
  const cacheKey = useMemo(() => pathname, [pathname]);

  // Login skips every provider but i18n (see the file header)
  if (pathname === '/login') {
    return <IntlProvider>{outlet}</IntlProvider>;
  }

  return (
    <AuthProvider>
      <Providers>
        <KeepAlive activeCacheKey={cacheKey} max={15} exclude={[/^\/login/]}>
          {outlet}
        </KeepAlive>
      </Providers>
    </AuthProvider>
  );
}
