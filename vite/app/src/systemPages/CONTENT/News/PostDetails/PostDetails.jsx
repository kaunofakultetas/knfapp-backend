// -----------------------------------------------------------
//  [*] CONTENT — PostDetails modal
//
//  One post up close: the full text, its counters, the
//  source link for scraped articles, and the hold-to-confirm
//  delete (DELETE /api/news/<id> — the moderation lever for
//  ANY post: the backend allows author-or-admin). Deleting a
//  scraped article also tombstones its source URL so the
//  scraper can never resurrect it.
//
//  Split into (root component last):
//
//    FactRow     — one label/value line
//    PostDetails — the dialog (default export)
// -----------------------------------------------------------

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useMemo } from 'react';

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
//   - PostDetails (below)
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
// PostDetails (default export)
// -----------------------------------------------------------
//
// The parent mounts/unmounts this component instead of
// toggling `open`. Curators can look but the delete is
// admin/author-only — the backend answers a stranger
// curator's delete with 403, so the button hides for them.
//
// Used by:
//   - NewsTable — opened on row click
// -----------------------------------------------------------

export default function PostDetails({ row, authData, onClose }) {

  const t = useTranslations("PAGES.news");
  const queryClient = useQueryClient();

  // Who may delete: the backend allows author-or-admin
  const canDelete = authData?.role === 'admin' || authData?.id === row.authorId;


  const remove = useMutation({
    mutationFn: async () => (await api.delete(`/api/news/${row.id}`)).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['news', 'list'] });
      onClose();
    },
  });


  // A scraped article deleted here never comes back — worth
  // saying out loud before the hold completes
  const scraped = useMemo(() => !['faculty', 'user'].includes(row.source), [row.source]);


  return (
    <UniversalModal
      open={true}   // always open — the parent mounts/unmounts this component instead
      onClose={onClose}
      title={row.title || t("untitled")}
      description={`${row.author} · ${formatDateTime(row.date)}`}
      maxWidth={560}
      fullWidth
      actions={canDelete ? (
        <LongPressDeleteButton
          duration={1500}
          fullWidth
          disabled={remove.isPending}
          tooltip={scraped ? t("DELETE.scraped_tooltip") : ""}
          onComplete={() => remove.mutate()}
          completedToastMessage={t("DELETE.completed_toast")}
          uncompletedToastMessage={t("DELETE.uncompleted_toast")}
        >
          {t("DELETE.button")}
        </LongPressDeleteButton>
      ) : undefined}
      showCancel={!canDelete}
      showConfirm={false}
    >
      <div className="flex flex-col gap-2">

        <FactRow label={t("COLUMNS.source")}>{row.source}</FactRow>
        <FactRow label={t("COLUMNS.postType")}>{t(`TYPES.${row.postType}`)}</FactRow>
        <FactRow label={t("COLUMNS.visibility")}>
          {row.isPublic ? t("VISIBILITY.public") : t("VISIBILITY.draft")}
        </FactRow>
        <FactRow label={t("counters")}>
          {t("counters_line", { likes: row.likes, comments: row.comments, shares: row.shares })}
        </FactRow>
        {row.sourceUrl && (
          <FactRow label={t("source_url")}>
            <a href={row.sourceUrl} target="_blank" rel="noopener noreferrer"
               className="text-primary underline break-all">
              {row.sourceUrl}
            </a>
          </FactRow>
        )}

        {/* The full text, scrollable for long articles */}
        <div className="text-sm bg-gray-50 border border-edge rounded-lg px-3 py-2 whitespace-pre-wrap max-h-[300px] overflow-y-auto">
          {row.content}
        </div>

      </div>
    </UniversalModal>
  );
}
