// -----------------------------------------------------------
//  [*] Others — Login page
//
//  The entry point of the panel: a white sign-in card
//  (username/email + password) on the animated particles
//  background. The same account works in the mobile app —
//  but only admin and curator accounts are let into the
//  panel; a valid student/teacher login is refused here
//  without storing its token.
//
//  Opening /login also acts as logout: the stored bearer
//  token is revoked server-side (best effort) and dropped
//  from localStorage on mount. After a successful login the
//  page hard-navigates to the panel root and the router
//  takes over with the fresh session.
//
//  This page styles itself — on /login App wraps the outlet
//  in the IntlProvider ONLY, never the auth or theme
//  providers. So colors must be hardcoded (bg-[#7B003F]) —
//  theme-based classes like bg-primary resolve to
//  --mui-palette-* variables that only exist inside the
//  ThemeProvider, so they render as nothing (white) here.
//
//  Split into (root component last):
//
//    PANEL_ROLES     — who may enter (mirror of AuthGuard)
//    LocaleToggle    — Lietuvių | English links on the card
//    LoginFields     — username + password inputs
//    SubmitButton    — sign-in button / disabled waiting state
//    CopyrightFooter — bottom copyright line
//    Login           — state + login request (default export)
// -----------------------------------------------------------

import { useState, useEffect, useCallback } from "react";
import { useMutation } from '@tanstack/react-query';
import axios from "axios";
import { errorText } from "@/api/client";
import { getToken, setToken, clearToken } from "@/auth/token";
import { useTranslations, useLocale, useSetLocale } from '@/i18n';

import { TextField } from "@mui/material";

import BouncingDotsLoader from './components/BouncingDotsLoader/BouncingDotsLoader';
import Particles from './components/Particles/Particles';


// Kept in sync with AuthGuard.PANEL_ROLES (not imported —
// pulling AuthGuard in here would drag the query provider
// context into the one page rendered without it)
const PANEL_ROLES = ['admin', 'curator'];







// -----------------------------------------------------------
// LocaleToggle
// -----------------------------------------------------------
//
// "Lietuvių | English" links in the card's top-right corner:
// the current locale is plain grey text, the other one a
// burgundy link that switches via setLocale (writes the
// cookie, re-renders the page). Hardcoded colors — no theme
// on this page.
//
// Used by:
//   - Login (below)
// -----------------------------------------------------------

function LocaleToggle() {
  const locale = useLocale();
  const setLocale = useSetLocale();
  const nt = useTranslations("navbar");

  const option = (code, label) => (
    locale === code
      ? <span className="text-gray-500">{label}</span>
      : <button type="button" onClick={() => setLocale(code)}
                className="text-[#7B003F] hover:underline bg-transparent border-none p-0 cursor-pointer">
          {label}
        </button>
  );

  return (
    <div className="text-xs text-right">
      {option('lt', nt("lithuanian"))} <span className="text-gray-400">|</span> {option('en', nt("english"))}
    </div>
  );
}







// -----------------------------------------------------------
// LoginFields
// -----------------------------------------------------------
//
// The username/email and password inputs. Submitting on Enter
// is handled globally by the page, so the fields only report
// their values up.
//
// Used by:
//   - Login (below)
// -----------------------------------------------------------

function LoginFields({ onUsernameChange, onPasswordChange, t }) {
  return (
    <div className="flex flex-col gap-4 mt-4 mb-14">
      <TextField
        required
        variant="standard"
        label={t("username")}
        onChange={(e) => onUsernameChange(e.currentTarget.value)}
        fullWidth
      />
      <TextField
        required
        variant="standard"
        type="password"
        label={t("password")}
        onChange={(e) => onPasswordChange(e.currentTarget.value)}
        fullWidth
      />
    </div>
  );
}







// -----------------------------------------------------------
// SubmitButton
// -----------------------------------------------------------
//
// The login button. While the request is in flight it turns
// into a disabled grey "please wait" with the bouncing dots
// loader; otherwise the burgundy "sign in". The burgundy is
// a hardcoded hex on purpose — see the file header.
//
// Used by:
//   - Login (below)
// -----------------------------------------------------------

function SubmitButton({ loggingIn, onClick, t }) {

  if (loggingIn) {
    return (
      <button
        disabled
        className="bg-gray-400 text-white py-2.5 px-4 rounded font-medium cursor-not-allowed flex items-center justify-center gap-2"
      >
        {t("please_wait")} <BouncingDotsLoader />
      </button>
    );
  }

  return (
    <button
      type="button"
      tabIndex={0}
      onClick={onClick}
      className="bg-[#7B003F] hover:bg-[#E64164] text-white py-2.5 px-4 rounded font-medium transition-colors cursor-pointer border-none outline-none focus-visible:ring-2 focus-visible:ring-[#E64164] focus-visible:ring-offset-2"
    >
      {t("sign_in")}
    </button>
  );
}







// -----------------------------------------------------------
// CopyrightFooter
// -----------------------------------------------------------
//
// Copyright line at the bottom of the screen.
//
// Used by:
//   - Login (below)
// -----------------------------------------------------------

function CopyrightFooter({ t }) {
  return (
    <div className="py-4 text-center relative z-10">
      <div className="text-white text-xs">
        {t("copyright")}
      </div>
    </div>
  );
}







// -----------------------------------------------------------
// Login (default export)
// -----------------------------------------------------------
//
// The page itself: revokes and drops the stored session on
// mount (logout), holds the credentials and does the login
// call. Enter submits from anywhere on the page.
//
// Used by:
//   - router.jsx — route /login (rendered without PageWrapper)
// -----------------------------------------------------------

export default function Login() {

  const t = useTranslations("PAGES.login");

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [errorBoxText, setErrorBoxText] = useState("");


  // Visiting /login logs the user out — revoke the session
  // server-side (best effort; raw axios, the shared client
  // would redirect on a stale token's 401) and drop the token
  useEffect(() => {
    const token = getToken();
    if (token) {
      axios.post("/api/auth/logout", {}, { headers: { Authorization: `Bearer ${token}` } }).catch(() => {});
      clearToken();
    }
  }, []);


  // The backend answers {user, token}. Deliberately RAW
  // axios, not the shared api client: a wrong password is a
  // 401, and the client's interceptor would hard-reload
  // /login instead of letting the error box show the reason.
  // A valid login with a non-staff role is refused HERE — the
  // token is never stored, so the account gains nothing.
  // On success a full page load restarts the app with the
  // fresh session — loggingIn stays true (isSuccess) so the
  // button keeps its waiting state until navigation.
  const login = useMutation({
    mutationFn: () => axios.post("/api/auth/login", { username, password }),
    onSuccess: (response) => {
      const { user, token } = response.data;
      if (!PANEL_ROLES.includes(user?.role)) {
        setErrorBoxText(t("not_staff"));
        return;
      }
      setToken(token);
      window.location.href = import.meta.env.BASE_URL;
    },
    onError: (error) => setErrorBoxText(errorText(error)),
  });
  const loggingIn = login.isPending || (login.isSuccess && Boolean(getToken()));
  const { mutate } = login;

  // The guard stops a double submit when Enter lands on the
  // focused button (its native click + the global listener)
  const handleLogin = useCallback(() => {
    if (loggingIn) return;
    setErrorBoxText("");
    mutate();
  }, [loggingIn, mutate]);


  // Enter submits from anywhere on the page
  useEffect(() => {
    const handleKeyDown = (event) => {
      if (event.key === 'Enter') handleLogin();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleLogin]);


  return (
    <div
      className="min-h-screen w-full flex flex-col relative"
      style={{ backgroundImage: "linear-gradient(to bottom right, #7b4397, #dc2430)" }}
    >
      {/* Animated background — behind everything, clicks pass through */}
      <div className="fixed inset-0 z-0 pointer-events-none">
        <Particles />
      </div>

      {/* Login card */}
      <div className="flex-1 flex items-center justify-center p-4 pb-16 relative z-10">
        <form className="w-full max-w-[350px] flex flex-col bg-white p-5 rounded-2xl shadow-2xl">
          <LocaleToggle />
          <img alt="VU KnF Logo" src={`${import.meta.env.BASE_URL}img/vuknflogo.png`} width={330} height={192} />

          <div className="text-center mt-3">
            <h1 className="text-lg font-medium text-gray-700">{t("title")}</h1>
          </div>

          <LoginFields
            onUsernameChange={setUsername}
            onPasswordChange={setPassword}
            t={t}
          />

          {/* The backend's error message (hidden when empty) */}
          {errorBoxText && (
            <div className="text-xs text-red-500 text-center whitespace-pre-wrap mb-2">
              {errorBoxText}
            </div>
          )}

          <SubmitButton loggingIn={loggingIn} onClick={handleLogin} t={t} />
        </form>
      </div>

      <CopyrightFooter t={t} />
    </div>
  );
}
