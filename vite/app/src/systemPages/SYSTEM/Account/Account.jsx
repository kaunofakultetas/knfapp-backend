// -----------------------------------------------------------
//  [*] SYSTEM — Account
//
//  The signed-in account's self-service page: who am I,
//  change my password, and the emergency brake — log out
//  every session everywhere.
//
//  Password change (POST /api/auth/change-password) kills
//  every OTHER session on success (the backend's rule — a
//  changed password means the old sessions are suspect), so
//  the page says so next to the form. A wrong old password
//  is a 400 with prose, toasted by the api client.
//
//  "Log out everywhere" (POST /api/auth/logout-all) also
//  revokes the session in hand — the page drops the token
//  and returns to /login.
//
//  Split into (root component last):
//
//    ProfileCard    — the account facts
//    PasswordForm   — old/new/repeat + submit
//    Account        — the page itself (default export)
// -----------------------------------------------------------

import { useState } from "react";
import { useMutation } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { Button, TextField } from "@mui/material";
import PersonIcon from '@mui/icons-material/Person';

import api from '@/api/client';
import { clearToken } from '@/auth/token';
import { useTranslations } from '@/i18n';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import LongPressButton from '@/components/LongPressButton';







// -----------------------------------------------------------
// ProfileCard
// -----------------------------------------------------------
//
// The account facts: display name, username, email, role.
//
// Used by:
//   - Account (below)
// -----------------------------------------------------------

function ProfileCard({ authData, t }) {
  return (
    <div className="rounded-[15px] bg-white p-5 shadow-card flex items-center gap-4">
      <div className="flex items-center justify-center w-14 h-14 rounded-full bg-primary">
        <PersonIcon sx={{ color: 'white', fontSize: 32 }} />
      </div>
      <div>
        <div className="text-lg font-semibold">{authData?.displayName}</div>
        <div className="text-sm text-muted">
          {authData?.username} · {authData?.email}
        </div>
        <div className="text-xs text-muted mt-1">
          {authData?.role ? t(`ROLES.${authData.role}`) : ''}
        </div>
      </div>
    </div>
  );
}







// -----------------------------------------------------------
// PasswordForm
// -----------------------------------------------------------
//
// The change-password card. The repeat field is checked
// client-side (the backend never sees it); the strength
// rules live on the backend and come back as prose when
// violated.
//
// Used by:
//   - Account (below)
// -----------------------------------------------------------

function PasswordForm({ t }) {

  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [repeatPassword, setRepeatPassword] = useState("");


  const change = useMutation({
    mutationFn: async () => (await api.post('/api/auth/change-password', {
      old_password: oldPassword,
      new_password: newPassword,
    })).data,
    onSuccess: () => {
      toast.success(<b>{t("PASSWORD.changed_toast")}</b>, { duration: 4000 });
      setOldPassword("");
      setNewPassword("");
      setRepeatPassword("");
    },
  });


  const mismatch = repeatPassword !== "" && newPassword !== repeatPassword;
  const submittable = oldPassword !== "" && newPassword !== ""
    && newPassword === repeatPassword && !change.isPending;


  return (
    <div className="rounded-[15px] bg-white p-5 shadow-card flex flex-col gap-4">

      <div className="font-semibold">{t("PASSWORD.title")}</div>
      <div className="text-xs text-muted -mt-2">{t("PASSWORD.sessions_note")}</div>

      <TextField
        type="password"
        label={t("PASSWORD.old")}
        value={oldPassword}
        onChange={(e) => setOldPassword(e.target.value)}
        fullWidth
      />

      <TextField
        type="password"
        label={t("PASSWORD.new")}
        value={newPassword}
        onChange={(e) => setNewPassword(e.target.value)}
        fullWidth
      />

      <TextField
        type="password"
        label={t("PASSWORD.repeat")}
        value={repeatPassword}
        onChange={(e) => setRepeatPassword(e.target.value)}
        error={mismatch}
        helperText={mismatch ? t("PASSWORD.mismatch") : ""}
        fullWidth
      />

      <Button
        variant="contained"
        disabled={!submittable}
        onClick={() => change.mutate()}
        sx={{ alignSelf: 'flex-end' }}
      >
        {t("PASSWORD.submit")}
      </Button>

    </div>
  );
}







// -----------------------------------------------------------
// Account (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /account (via PageWrapper)
// -----------------------------------------------------------

export default function Account({ authData }) {

  const t = useTranslations("PAGES.account");


  // Revokes every session (this one included), then restarts
  // at the login page with a clean slate
  const logoutAll = useMutation({
    mutationFn: async () => (await api.post('/api/auth/logout-all')).data,
    onSuccess: () => {
      clearToken();
      window.location.href = `${import.meta.env.BASE_URL}login`;
    },
  });


  return (
    <PageLayout authData={authData}>
      <div className="p-5 max-w-[560px] flex flex-col gap-4">

        <PageTitle>{t("TITLE")}</PageTitle>

        <ProfileCard authData={authData} t={t} />

        <PasswordForm t={t} />

        {/* The emergency brake — every device, every session */}
        <div className="rounded-[15px] bg-white p-5 shadow-card flex flex-col gap-3">
          <div className="font-semibold">{t("LOGOUT_ALL.title")}</div>
          <div className="text-xs text-muted">{t("LOGOUT_ALL.note")}</div>
          <LongPressButton
            duration={2000}
            color="error"
            disabled={logoutAll.isPending}
            onComplete={() => logoutAll.mutate()}
            uncompletedToastMessage={t("LOGOUT_ALL.uncompleted_toast")}
            sx={{ alignSelf: 'flex-start' }}
          >
            {t("LOGOUT_ALL.button")}
          </LongPressButton>
        </div>

      </div>
    </PageLayout>
  );
}
