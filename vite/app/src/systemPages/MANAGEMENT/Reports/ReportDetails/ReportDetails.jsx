// -----------------------------------------------------------
//  [*] MANAGEMENT — ReportDetails modal
//
//  One report up close: who reported what and why, plus the
//  resolve/reopen action (PUT /api/admin/reports/<id>).
//
//  For a reported POST the dialog additionally fetches the
//  post itself (GET /api/news/<id>) to show its title and
//  author; for a reported MESSAGE it uses the moderation
//  window (GET /api/admin/messages/<id> — the one chat read
//  that is not members-only) and shows the text with its
//  sender, unsent stamp included. A 404 on either fetch
//  means the target is already gone, which is worth seeing
//  too.
//
//  Split into (root component last):
//
//    FactRow        — one label/value line
//    TargetPreview  — the reported post's title/author card
//    MessagePreview — the reported message's text/sender card
//    ReportDetails  — the dialog (default export)
// -----------------------------------------------------------

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { Button } from "@mui/material";

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { formatDateTime } from '@/utils/timestamps';
import UniversalModal from '@/components/UniversalModal';







// -----------------------------------------------------------
// FactRow
// -----------------------------------------------------------
//
// One "label: value" line of the details list.
//
// Used by:
//   - ReportDetails (below)
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
// TargetPreview
// -----------------------------------------------------------
//
// The reported post at a glance (title + author), or the
// "already gone" note when the fetch came back 404.
//
// Used by:
//   - ReportDetails (below) — only for targetType "post"
// -----------------------------------------------------------

function TargetPreview({ post, failed, t }) {

  if (failed) {
    return (
      <div className="text-xs text-muted bg-gray-50 border border-edge rounded-lg px-3 py-2">
        {t("target_gone")}
      </div>
    );
  }

  if (!post) return null;

  return (
    <div className="text-sm bg-gray-50 border border-edge rounded-lg px-3 py-2">
      <div className="font-semibold">{post.title || post.content?.slice(0, 80)}</div>
      <div className="text-xs text-muted mt-1">{post.author} · {formatDateTime(post.date)}</div>
    </div>
  );
}







// -----------------------------------------------------------
// MessagePreview
// -----------------------------------------------------------
//
// The reported chat message at a glance: sender, time, the
// text itself — with the unsent marker when the author
// already deleted it (the backend still serves it here for
// exactly this view).
//
// Used by:
//   - ReportDetails (below) — only for targetType "message"
// -----------------------------------------------------------

function MessagePreview({ message, failed, t }) {

  if (failed) {
    return (
      <div className="text-xs text-muted bg-gray-50 border border-edge rounded-lg px-3 py-2">
        {t("target_gone")}
      </div>
    );
  }

  if (!message) return null;

  return (
    <div className="text-sm bg-gray-50 border border-edge rounded-lg px-3 py-2">
      <div className="text-xs text-muted">
        {message.senderName} · {formatDateTime(message.createdAt)}
        {message.deletedAt && (
          <span className="ml-2 text-red-600 font-semibold">{t("message_unsent")}</span>
        )}
      </div>
      <div className="mt-1 whitespace-pre-wrap">{message.text}</div>
      {message.attachmentName && (
        <div className="text-xs text-muted mt-1">📎 {message.attachmentName}</div>
      )}
    </div>
  );
}







// -----------------------------------------------------------
// ReportDetails (default export)
// -----------------------------------------------------------
//
// The parent mounts/unmounts this component instead of
// toggling `open`.
//
// Used by:
//   - ReportsTable — opened on row click
// -----------------------------------------------------------

export default function ReportDetails({ row, onClose }) {

  const t = useTranslations("PAGES.reports");
  const queryClient = useQueryClient();

  const isOpen = row.status === 'open';


  // The reported post, when the target is one — a 404 is a
  // meaningful answer here, not an error to retry
  const { data: post, isError: postGone } = useQuery({
    queryKey: ['news', 'post', row.targetId],
    queryFn: async () => (await api.get(`/api/news/${row.targetId}`)).data,
    enabled: row.targetType === 'post',
    retry: false,
  });


  // The reported message, through the moderation window
  const { data: message, isError: messageGone } = useQuery({
    queryKey: ['admin', 'message', row.targetId],
    queryFn: async () => (await api.get(`/api/admin/messages/${row.targetId}`)).data,
    enabled: row.targetType === 'message',
    retry: false,
  });


  const setStatus = useMutation({
    mutationFn: async (status) =>
      (await api.put(`/api/admin/reports/${row.id}`, { status })).data,
    onSuccess: () => {
      toast.success(<b>{t("status_saved_toast")}</b>, { duration: 3000 });
      queryClient.invalidateQueries({ queryKey: ['admin', 'reports'] });
      onClose();
    },
  });


  return (
    <UniversalModal
      open={true}   // the parent mounts/unmounts instead of toggling
      onClose={onClose}
      title={t("details_title")}
      variant={isOpen ? "warning" : "default"}
      maxWidth={460}
      fullWidth
      actions={
        <Button
          variant="contained"
          color={isOpen ? "success" : "warning"}
          fullWidth
          disabled={setStatus.isPending}
          onClick={() => setStatus.mutate(isOpen ? 'resolved' : 'open')}
        >
          {isOpen ? t("resolve") : t("reopen")}
        </Button>
      }
    >
      <div className="flex flex-col gap-2">

        <FactRow label={t("COLUMNS.createdAt")}>{formatDateTime(row.createdAt)}</FactRow>
        <FactRow label={t("COLUMNS.reporter")}>{row.reporterName}</FactRow>
        <FactRow label={t("COLUMNS.targetType")}>{t(`TARGETS.${row.targetType}`)}</FactRow>
        {row.targetUserName && (
          <FactRow label={t("COLUMNS.targetUser")}>{row.targetUserName}</FactRow>
        )}
        <FactRow label={t("target_id")}>
          <span className="font-mono text-xs">{row.targetId}</span>
        </FactRow>

        {/* The reported post or message, when it can be shown */}
        {row.targetType === 'post' && (
          <TargetPreview post={post} failed={postGone} t={t} />
        )}
        {row.targetType === 'message' && (
          <MessagePreview message={message} failed={messageGone} t={t} />
        )}

        {/* The reporter's own words */}
        <div className="text-sm bg-gray-50 border border-edge rounded-lg px-3 py-2 whitespace-pre-wrap">
          {row.reason}
        </div>

      </div>
    </UniversalModal>
  );
}
