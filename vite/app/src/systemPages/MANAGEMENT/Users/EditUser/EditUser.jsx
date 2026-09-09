// -----------------------------------------------------------
//  [*] MANAGEMENT — EditUser modal
//
//  The account dialog opened from the users grid. Two levers
//  and one hard action:
//    - role   — select over the four roles
//    - active — deactivating kills the account's sessions
//               and push tokens instantly (the backend does
//               that inside the same request)
//    - erase  — hold-to-confirm GDPR erasure: the account is
//               anonymised in place, personal rows removed;
//               there is NO undo
//
//  The backend refuses self-destructive edits (deactivating
//  yourself, dropping your own admin role, removing the last
//  active admin) — the dialog additionally disables the
//  levers on the caller's own row so the refusal is visible
//  before the request.
//
//  Split into (root component last):
//
//    ActionButtons — footer: Save + hold-to-erase
//    EditUser      — state + API calls (default export)
// -----------------------------------------------------------

import { useState } from "react";
import { useMutation, useQueryClient } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { Button, MenuItem, TextField, FormControlLabel } from "@mui/material";

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import UniversalModal from '@/components/UniversalModal';
import { LongPressDeleteButton } from '@/components/LongPressButton';
import IOSSwitch from '@/components/other/IOSSwitch/IOSSwitch';


const ROLES = ['student', 'teacher', 'curator', 'admin'];







// -----------------------------------------------------------
// ActionButtons
// -----------------------------------------------------------
//
// Modal footer: the Save button, and the hold-to-confirm
// erasure button (disabled on the caller's own row — the
// backend points self-deletion at the account's own flow).
//
// Used by:
//   - EditUser (below) — the modal's `actions` slot
// -----------------------------------------------------------

function ActionButtons({ onSave, onErase, saving, isSelf, t }) {
  return (
    <div className="flex gap-3">
      <LongPressDeleteButton
        duration={3000}
        sx={{ flex: 1 }}
        disabled={isSelf || saving}
        tooltip={isSelf ? t("ERASE.self_tooltip") : t("ERASE.tooltip")}
        onComplete={onErase}
        completedToastMessage={t("ERASE.completed_toast")}
        uncompletedToastMessage={t("ERASE.uncompleted_toast")}
      >
        {t("ERASE.button")}
      </LongPressDeleteButton>

      <Button variant="contained" sx={{ flex: 1 }} disabled={saving} onClick={onSave}>
        {t("save")}
      </Button>
    </div>
  );
}







// -----------------------------------------------------------
// EditUser (default export)
// -----------------------------------------------------------
//
// Holds the edited role/active pair and the two mutations.
// The parent mounts/unmounts this component instead of
// toggling `open`.
//
// Used by:
//   - UsersTable — opened on row click
// -----------------------------------------------------------

export default function EditUser({ row, authData, onClose }) {

  const t = useTranslations("PAGES.users");
  const queryClient = useQueryClient();

  const isSelf = row.id === authData?.id;

  const [role, setRole] = useState(row.role);
  const [active, setActive] = useState(Boolean(row.active));


  // Only the changed fields go on the wire — the backend
  // audits every field it receives
  const save = useMutation({
    mutationFn: async () => {
      const patch = {};
      if (role !== row.role) patch.role = role;
      if (active !== Boolean(row.active)) patch.active = active;
      return (await api.patch(`/api/admin/users/${row.id}`, patch)).data;
    },
    onSuccess: () => {
      toast.success(<b>{t("saved_toast")}</b>, { duration: 3000 });
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] });
      onClose();
    },
  });


  const erase = useMutation({
    mutationFn: async () => (await api.delete(`/api/admin/users/${row.id}`)).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'users'] });
      onClose();
    },
  });


  const nothingChanged = role === row.role && active === Boolean(row.active);

  const handleSave = () => {
    if (nothingChanged) {
      onClose();
      return;
    }
    save.mutate();
  };


  return (
    <UniversalModal
      open={true}   // the parent mounts/unmounts instead of toggling
      onClose={onClose}
      title={row.displayName || row.username}
      description={`${row.username} · ${row.email}`}
      maxWidth={440}
      fullWidth
      actions={
        <ActionButtons
          onSave={handleSave}
          onErase={() => erase.mutate()}
          saving={save.isPending || erase.isPending}
          isSelf={isSelf}
          t={t}
        />
      }
    >
      <div className="flex flex-col gap-4 pt-1">

        {/* Role */}
        <TextField
          select
          label={t("COLUMNS.role")}
          value={role}
          onChange={(e) => setRole(e.target.value)}
          disabled={isSelf}   // the backend refuses dropping your own admin role
          fullWidth
        >
          {ROLES.map((r) => (
            <MenuItem key={r} value={r}>{t(`ROLES.${r}`)}</MenuItem>
          ))}
        </TextField>

        {/* Active */}
        <FormControlLabel
          sx={{ ml: 0 }}
          control={
            <IOSSwitch
              checked={active}
              disabled={isSelf}   // the backend refuses deactivating yourself
              onChange={(e) => setActive(e.target.checked)}
              sx={{ marginRight: '10px' }}
            />
          }
          label={active ? t("STATE.active") : t("STATE.inactive")}
        />

        {/* Deactivation kicks the account out immediately */}
        {!active && Boolean(row.active) && (
          <div className="text-xs text-orange-700 bg-orange-50 border border-orange-200 rounded-lg px-3 py-2">
            {t("deactivate_warning")}
          </div>
        )}

      </div>
    </UniversalModal>
  );
}
