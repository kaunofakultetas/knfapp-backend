// -----------------------------------------------------------
//  [*] MANAGEMENT — InvitationDetails modal
//
//  One invitation code up close: the code in monospace with
//  a copy button, its facts (role, uses, validity), and the
//  hold-to-confirm revoke. Revoking is a hard delete — a
//  code already handed out simply stops registering anyone.
//
//  Split into (root component last):
//
//    FactRow           — one label/value line
//    InvitationDetails — the dialog (default export)
// -----------------------------------------------------------

import { useMutation, useQueryClient } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { IconButton } from "@mui/material";
import ContentCopyIcon from '@mui/icons-material/ContentCopy';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { formatDateTime } from '@/utils/timestamps';
import UniversalModal from '@/components/UniversalModal';
import { LongPressDeleteButton } from '@/components/LongPressButton';







// -----------------------------------------------------------
// FactRow
// -----------------------------------------------------------
//
// One "label: value" line of the details list.
//
// Used by:
//   - InvitationDetails (below)
// -----------------------------------------------------------

function FactRow({ label, children }) {
  return (
    <div className="flex justify-between gap-4 text-sm py-1">
      <span className="text-muted">{label}</span>
      <span className="font-medium text-right">{children}</span>
    </div>
  );
}







// -----------------------------------------------------------
// InvitationDetails (default export)
// -----------------------------------------------------------
//
// The parent mounts/unmounts this component instead of
// toggling `open`.
//
// Used by:
//   - InvitationsTable — opened on row click
// -----------------------------------------------------------

export default function InvitationDetails({ row, onClose }) {

  const t = useTranslations("PAGES.invitations");
  const queryClient = useQueryClient();


  const revoke = useMutation({
    mutationFn: async () => (await api.delete(`/api/admin/invitations/${row.id}`)).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'invitations'] });
      onClose();
    },
  });


  const copyCode = () => {
    navigator.clipboard.writeText(row.code)
      .then(() => toast.success(<b>{t("copied_toast")}</b>, { duration: 2000 }))
      .catch(() => {});
  };


  return (
    <UniversalModal
      open={true}   // always open — the parent mounts/unmounts this component instead
      onClose={onClose}
      title={t("details_title")}
      maxWidth={420}
      fullWidth
      actions={
        <LongPressDeleteButton
          duration={1500}
          fullWidth
          disabled={revoke.isPending}
          onComplete={() => revoke.mutate()}
          completedToastMessage={t("REVOKE.completed_toast")}
          uncompletedToastMessage={t("REVOKE.uncompleted_toast")}
        >
          {t("REVOKE.button")}
        </LongPressDeleteButton>
      }
    >
      <div className="flex flex-col gap-2">

        {/* The code itself + copy */}
        <div className="flex items-center justify-center gap-2 bg-gray-100 rounded-lg py-2 mb-2">
          <span className="font-mono text-xl tracking-[0.25em]">{row.code}</span>
          <IconButton size="small" onClick={copyCode}>
            <ContentCopyIcon fontSize="small" />
          </IconButton>
        </div>

        <FactRow label={t("COLUMNS.role")}>{t(`ROLES.${row.role}`)}</FactRow>
        <FactRow label={t("COLUMNS.uses")}>{row.useCount} / {row.maxUses}</FactRow>
        <FactRow label={t("COLUMNS.expiresAt")}>{formatDateTime(row.expiresAt)}</FactRow>
        <FactRow label={t("COLUMNS.createdAt")}>{formatDateTime(row.createdAt)}</FactRow>

      </div>
    </UniversalModal>
  );
}
