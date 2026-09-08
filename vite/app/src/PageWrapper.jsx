// -----------------------------------------------------------
//  [*] PageWrapper — injects authData into each page
//
//  Thin bridge between the route table and the pages: reads
//  the session from AuthGuard and renders the page with
//  authData as a prop, so pages can gate admin-only actions
//  on authData.role without reaching for the context
//  themselves.
//
//  Used by:
//    - router.jsx — wraps every page except /login
// -----------------------------------------------------------

import { useAuth } from '@/AuthGuard';


export default function PageWrapper({ component: Component }) {
  const authData = useAuth();

  return <Component authData={authData} />;
}
