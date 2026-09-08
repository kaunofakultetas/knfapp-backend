// -----------------------------------------------------------
//  [*] Navbar — the burgundy top bar
//
//  Shown on every page: VU logo linking to /, the app title,
//  and on the right a user mini widget (display name + role,
//  links to /account), the language switcher and the logout
//  button.
//
//  Logging out is just a hard navigation to /login — the
//  login page drops the session server-side on mount.
//
//  Split into (root component last):
//
//    UserWidget       — display name + translated role
//    LanguageSwitcher — offers the "other" language
//    Navbar           — the bar itself (default export)
// -----------------------------------------------------------

import { Link } from "react-router-dom";
import { Box } from '@mui/material';
import PersonIcon from '@mui/icons-material/Person';

import { useTranslations, useLocale, useSetLocale } from "@/i18n";







// -----------------------------------------------------------
// UserWidget
// -----------------------------------------------------------
//
// The signed-in account at a glance: display name on top,
// translated role underneath; the whole chip links to the
// account page. Falls back to a generic label while the
// session check is still running.
//
// Used by:
//   - Navbar (below)
// -----------------------------------------------------------

function UserWidget({ authData, t }) {
  return (
    <Link to="/account" style={{ textDecoration: "none" }}>
      <div className="flex items-center gap-3 mr-5 py-2 px-4 rounded-lg bg-white/10 border border-white/20">
        <PersonIcon style={{ color: 'white', fontSize: '24px' }} />
        <div className="flex flex-col items-start">
          <span className="text-white text-[0.85em] font-semibold leading-tight">
            {authData?.displayName || t("user")}
          </span>
          <span className="text-white/70 text-[0.7em] leading-tight">
            {authData?.role ? t(`ROLES.${authData.role}`) : t("user")}
          </span>
        </div>
      </div>
    </Link>
  );
}







// -----------------------------------------------------------
// LanguageSwitcher
// -----------------------------------------------------------
//
// One button offering the language that is NOT active;
// clicking it switches the locale (cookie + re-render, no
// reload).
//
// Used by:
//   - Navbar (below)
// -----------------------------------------------------------

function LanguageSwitcher({ t }) {

  const locale = useLocale();
  const setLocale = useSetLocale();

  const target = locale === 'lt' ? 'en' : 'lt';

  return (
    <button
      type="button"
      onClick={() => setLocale(target)}
      className="mr-4 px-3 py-2 text-white text-sm rounded cursor-pointer bg-transparent border border-white/40 hover:bg-white/10 transition-colors"
    >
      {target === 'lt' ? t("lithuanian") : t("english")}
    </button>
  );
}







// -----------------------------------------------------------
// Navbar (default export)
// -----------------------------------------------------------
//
// Used by:
//   - PageLayout — above the sidebar/content on every page
// -----------------------------------------------------------

export default function Navbar({ authData }) {

  const t = useTranslations("navbar");

  return (
    <div className="h-[75px] flex items-center text-sm bg-primary border-b-[0.5px] border-b-[rgb(231,228,228)]">

      {/* VU logo */}
      <Link to="/" className="no-underline mx-4 sm:mx-[30px] shrink-0">
        <img src={`${import.meta.env.BASE_URL}img/vulogo.png`} alt="VU logotipas" className="h-[50px]" />
      </Link>

      <div className="w-full p-5 flex items-center justify-between gap-3">

        {/* App title — hidden on narrow screens, the logo is enough */}
        <Link to="/" className="no-underline hidden md:block">
          <div className="border border-white rounded-[15px] text-white py-2 px-3">
            {t("title")}
          </div>
        </Link>
        <div className="md:hidden" />

        {/* Right side of the navbar */}
        <div className="flex items-center mr-5">

          <UserWidget authData={authData} t={t} />

          <Box className="flex-grow justify-end lg:flex">
            <LanguageSwitcher t={t} />
          </Box>

          {/* Logout — the login page kills the session on mount */}
          <button
            className="px-4 py-2 text-white font-medium rounded cursor-pointer hover:opacity-90 transition-opacity"
            style={{
              background: 'var(--mui-palette-primary-main)',
              border: '1px solid rgba(255, 255, 255, 1)'
            }}
            onClick={() => { window.location.href = `${import.meta.env.BASE_URL}login`; }}
          >
            {t("logout")}
          </button>

        </div>
      </div>
    </div>
  );
}
