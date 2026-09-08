// -----------------------------------------------------------
//  [*] MANAGEMENT — NewInvitation modal
//
//  Mints an invitation code (POST /api/admin/invitations).
//  The form follows the backend's rules instead of
//  discovering them as errors:
//    - curators only see the student/teacher roles (their
//      whole minting scope)
//    - an admin/curator code is forced to a single use and
//      at most 72 h of validity — the backend refuses more
//    - everything else: 1–1000 uses, 1–8760 h
//
//  On success the dialog switches to a "code created" view
//  showing the code big, with a copy button — this is the
//  only moment the code is worth copying, so it must not
//  vanish behind an auto-close.
//
//  Split into (root component last):
//
//    CreatedView   — the code + copy button after minting
//    NewInvitation — the form + mutation (default export)
// -----------------------------------------------------------

import { useState } from "react";
import { useMutation, useQueryClient } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { Button, MenuItem, TextField } from "@mui/material";
import ContentCopyIcon from '@mui/icons-material/ContentCopy';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import UniversalModal from '@/components/UniversalModal';


// What the backend enforces for admin/curator codes
const PRIVILEGED_ROLES = ['admin', 'curator'];
const PRIVILEGED_EXPIRES_MAX = 72;







// -----------------------------------------------------------
// CreatedView
// -----------------------------------------------------------
//
// The success state: the fresh code in large monospace with
// a copy-to-clipboard button underneath.
//
// Used by:
//   - NewInvitation (below) — after the mint succeeded
// -----------------------------------------------------------

function CreatedView({ invitation, t }) {

  const copyCode = () => {
    navigator.clipboard.writeText(invitation.code)
      .then(() => toast.success(<b>{t("copied_toast")}</b>, { duration: 2000 }))
      .catch(() => {});
  };

  return (
    <div className="flex flex-col items-center gap-3 py-2">
      <div className="font-mono text-2xl tracking-[0.3em] bg-gray-100 rounded-lg px-4 py-3">
        {invitation.code}
      </div>
      <div className="text-xs text-muted">
        {t("created_hint", { role: t(`ROLES.${invitation.role}`) })}
      </div>
      <Button variant="outlined" startIcon={<ContentCopyIcon />} onClick={copyCode}>
        {t("copy_code")}
      </Button>
    </div>
  );
}







// -----------------------------------------------------------
// NewInvitation (default export)
// -----------------------------------------------------------
//
// The parent mounts/unmounts this component instead of
// toggling `open`.
//
// Used by:
//   - InvitationsTable — the toolbar's "new invitation"
//     button
// -----------------------------------------------------------

export default function NewInvitation({ authData, onClose }) {

  const t = useTranslations("PAGES.invitations");
  const queryClient = useQueryClient();

  const [role, setRole] = useState('student');
  const [maxUses, setMaxUses] = useState(1);
  const [expiresHours, setExpiresHours] = useState(168);
  const [created, setCreated] = useState(null);

  // Curators may only mint the everyday roles; minting staff
  // is an admin-only power
  const roleOptions = authData?.role === 'admin'
    ? ['student', 'teacher', 'curator', 'admin']
    : ['student', 'teacher'];

  const privileged = PRIVILEGED_ROLES.includes(role);


  const mint = useMutation({
    mutationFn: async () => (await api.post('/api/admin/invitations', {
      role,
      max_uses: privileged ? 1 : Number(maxUses),
      expires_hours: privileged
        ? Math.min(Number(expiresHours), PRIVILEGED_EXPIRES_MAX)
        : Number(expiresHours),
    })).data,
    onSuccess: (invitation) => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'invitations'] });
      setCreated(invitation);
    },
  });


  return (
    <UniversalModal
      open={true}   // always open — the parent mounts/unmounts this component instead
      onClose={onClose}
      title={created ? t("created_title") : t("new_invitation")}
      variant={created ? "success" : "default"}
      maxWidth={420}
      fullWidth
      showCancel={!created}
      showConfirm={!created}
      confirmText={t("mint")}
      closeOnConfirm={false}   // the success view must stay up to show the code
      loading={mint.isPending}
      onConfirm={() => mint.mutate()}
    >
      {created ? (
        <CreatedView invitation={created} t={t} />
      ) : (
        <div className="flex flex-col gap-4 pt-1">

          {/* Role */}
          <TextField
            select
            label={t("COLUMNS.role")}
            value={role}
            onChange={(e) => setRole(e.target.value)}
            fullWidth
          >
            {roleOptions.map((r) => (
              <MenuItem key={r} value={r}>{t(`ROLES.${r}`)}</MenuItem>
            ))}
          </TextField>

          {/* Max uses — pinned to 1 for staff codes */}
          <TextField
            type="number"
            label={t("COLUMNS.maxUses")}
            value={privileged ? 1 : maxUses}
            onChange={(e) => setMaxUses(e.target.value)}
            disabled={privileged}
            helperText={privileged ? t("privileged_uses_hint") : ""}
            slotProps={{ htmlInput: { min: 1, max: 1000 } }}
            fullWidth
          />

          {/* Validity in hours — staff codes cap at 72 h */}
          <TextField
            type="number"
            label={t("COLUMNS.expiresHours")}
            value={expiresHours}
            onChange={(e) => setExpiresHours(e.target.value)}
            helperText={privileged ? t("privileged_expiry_hint", { hours: PRIVILEGED_EXPIRES_MAX }) : t("expiry_hint")}
            slotProps={{ htmlInput: { min: 1, max: privileged ? PRIVILEGED_EXPIRES_MAX : 8760 } }}
            fullWidth
          />

        </div>
      )}
    </UniversalModal>
  );
}
