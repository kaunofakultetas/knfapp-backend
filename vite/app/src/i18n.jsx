// -----------------------------------------------------------
//  [*] i18n — lightweight translations (lt / en)
//
//  Hand-rolled replacement for an i18n library. Messages live
//  in src/messages (one JSON tree per locale, merged by
//  messages/index.js into messagesMap). The active locale is
//  remembered in the `locale` cookie; Lithuanian is the
//  default (the panel's audience is the faculty staff) and
//  the fallback for everything.
//
//  Exports:
//    IntlProvider     — context provider (wraps the app)
//    useLocale        — current locale code ("lt" | "en")
//    useSetLocale     — switch locale (also writes the cookie)
//    useTranslations  — t(key, params) for one namespace
//    translate        — t() outside React (api client toasts)
//
//  Typical use:
//    const t = useTranslations("PAGES.users");
//    t("TITLE")                 → "Naudotojai"
//    t("greeting", { name })    → fills {name} placeholders
// -----------------------------------------------------------

/* eslint-disable react-refresh/only-export-components --
   the provider and its hooks live in one module on purpose (see
   the header); the only cost is that editing this file reloads
   the page instead of hot-swapping it */

import { createContext, useContext, useState, useCallback } from 'react';
import { messagesMap } from '@/messages';


const DEFAULT_LOCALE = 'lt';
const SUPPORTED_LOCALES = ['lt', 'en'];

const ltMessages = messagesMap.lt;







// -----------------------------------------------------------
// getLocaleFromCookie
// -----------------------------------------------------------
//
// Initial locale: the `locale` cookie if it holds a supported
// value, otherwise Lithuanian.
//
// Used by:
//   - IntlProvider (below) — initial state
//   - translate (below) — outside React
// -----------------------------------------------------------

function getLocaleFromCookie() {
  const match = document.cookie.match(/(?:^|;\s*)locale=([^;]*)/);
  const locale = match ? match[1] : null;
  return SUPPORTED_LOCALES.includes(locale) ? locale : DEFAULT_LOCALE;
}



const IntlContext = createContext({
  locale: DEFAULT_LOCALE,
  messages: ltMessages,
  setLocale: () => {},
});







// -----------------------------------------------------------
// IntlProvider
// -----------------------------------------------------------
//
// Holds the live locale and the matching message tree.
// setLocale validates the value, persists it to the cookie
// and re-renders the app — no page reload.
//
// Used by:
//   - App.jsx — wraps every page (the login page included)
// -----------------------------------------------------------

export function IntlProvider({ children }) {

  const [locale, setLocaleState] = useState(getLocaleFromCookie);
  const messages = messagesMap[locale] || ltMessages;

  const setLocale = useCallback((newLocale) => {
    if (SUPPORTED_LOCALES.includes(newLocale)) {
      document.cookie = `locale=${newLocale}; path=/`;
      setLocaleState(newLocale);
    }
  }, []);

  return (
    <IntlContext.Provider value={{ locale, messages, setLocale }}>
      {children}
    </IntlContext.Provider>
  );
}







// -----------------------------------------------------------
// resolve
// -----------------------------------------------------------
//
// Walks a dot-separated path into the message tree:
//   resolve(messages, "sidebar.MANAGEMENT") → { TITLE, ... }
// Returns undefined as soon as any step is missing.
//
// Used by:
//   - useTranslations (below) — for the namespace and the key
//   - formatMessage (below)
// -----------------------------------------------------------

function resolve(obj, path) {
  const keys = path.split('.');
  let value = obj;
  for (const k of keys) {
    value = value?.[k];
    if (value === undefined) return undefined;
  }
  return value;
}







// -----------------------------------------------------------
// formatMessage
// -----------------------------------------------------------
//
// The one lookup+interpolation implementation: resolves a
// dotted key in a message tree and fills its {param}
// placeholders. A missing key comes back as the key itself
// (an untranslated string shows up literally instead of
// crashing); a key pointing at a subtree comes back as-is;
// placeholders with no matching param stay literal so the
// gap is visible.
//
// Used by:
//   - useTranslations (below) — scoped to a namespace
//   - translate (below) — whole tree, outside React
// -----------------------------------------------------------

function formatMessage(messages, key, params) {
  const value = resolve(messages, key);
  if (value === undefined) return key;
  if (typeof value !== 'string') return value;
  if (!params) return value;
  return value.replace(/\{(\w+)\}/g, (_, name) =>
    params[name] !== undefined ? params[name] : `{${name}}`
  );
}







// -----------------------------------------------------------
// translate
// -----------------------------------------------------------
//
// t() for code that runs outside React — the api client's
// error toasts. Reads the locale from the cookie and formats
// a FULL key into the message tree:
//
//   translate("navbar.ERRORS.REQUEST_FAILED", { status_code })
//
// Used by:
//   - api/client.js — errorText
// -----------------------------------------------------------

export function translate(key, params, locale) {
  const messages = messagesMap[locale || getLocaleFromCookie()] || ltMessages;
  return formatMessage(messages, key, params);
}







// -----------------------------------------------------------
// useTranslations
// -----------------------------------------------------------
//
// Returns t(key, params) scoped to a namespace. Missing
// namespaces/keys fall back to returning the key itself, so
// untranslated strings show up literally instead of crashing.
// {param} placeholders are filled from the params object;
// unknown placeholders are left as-is.
//
// Used by:
//   - Navbar ("navbar"), Sidebar ("sidebar"), and every page
//     with its tables and dialogs ("PAGES.<page>")
// -----------------------------------------------------------

export function useTranslations(namespace) {

  const { messages } = useContext(IntlContext);

  // The returned t() is memoized so components can safely list it
  // in hook dependencies; it only changes on locale switch
  return useCallback((key, params) => {

    // Narrow the message tree to the namespace, e.g. "sidebar"
    // → messages.sidebar. No namespace = whole tree.
    const section = namespace ? resolve(messages, namespace) : messages;
    if (!section) return key;   // unknown namespace → show the key

    // The key itself can be dotted too, e.g. "MANAGEMENT.users"
    return formatMessage(section, key, params);
  }, [messages, namespace]);
}







// -----------------------------------------------------------
// useLocale / useSetLocale
// -----------------------------------------------------------

// Current locale code ("lt" | "en")
//
// Used by:
//   - Navbar, Login — locale-aware language switcher
export function useLocale() {
  return useContext(IntlContext).locale;
}


// Switch the locale.
//
// Used by:
//   - Navbar, Login — the language switcher buttons
export function useSetLocale() {
  return useContext(IntlContext).setLocale;
}
