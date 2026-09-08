// -----------------------------------------------------------
//  [*] OPERATIONS — BuildingDetails modal
//
//  One building's lifecycle: the draft/published revisions,
//  the version history (GET .../versions), a link to the
//  published graph document, and — for admins — the publish
//  action (POST .../publish).
//
//  Publishing can be REFUSED by the backend:
//    - 422 "The draft has errors" carries a validation issue
//      list — it renders inside the dialog so the mapper
//      knows what to fix in the editor
//    - 409 "unchanged" means there is nothing new to publish
//  Both arrive through the api client's error toast too; the
//  422's issue list is the part worth keeping on screen.
//
//  Split into (root component last):
//
//    FactRow         — one label/value line
//    IssueList       — the 422 validation issues
//    VersionHistory  — the published revisions list
//    BuildingDetails — the dialog (default export)
// -----------------------------------------------------------

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { Button } from "@mui/material";
import OpenInNewIcon from '@mui/icons-material/OpenInNew';

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
//   - BuildingDetails (below)
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
// IssueList
// -----------------------------------------------------------
//
// The draft's validation issues after a refused publish:
// severity, the offending entity ref, and the message —
// straight from the backend's checker.
//
// Used by:
//   - BuildingDetails (below)
// -----------------------------------------------------------

function IssueList({ issues, t }) {

  if (!issues?.length) return null;

  return (
    <div className="text-xs bg-red-50 border border-red-200 rounded-lg px-3 py-2">
      <div className="font-semibold text-red-700 mb-1">{t("PUBLISH.refused")}</div>
      {issues.map((issue, index) => (
        <div key={index} className="text-red-600">
          [{issue.severity}] {issue.ref}: {issue.message}
        </div>
      ))}
    </div>
  );
}







// -----------------------------------------------------------
// VersionHistory
// -----------------------------------------------------------
//
// Every published revision, newest first: number, note,
// publisher, time, size.
//
// Used by:
//   - BuildingDetails (below)
// -----------------------------------------------------------

function VersionHistory({ versions, t }) {

  if (!versions?.length) {
    return <div className="text-xs text-muted">{t("VERSIONS.none")}</div>;
  }

  return (
    <div className="flex flex-col gap-1 max-h-[180px] overflow-y-auto">
      {versions.map((version) => (
        <div key={version.revision} className="flex justify-between gap-3 text-xs bg-gray-50 border border-edge rounded-lg px-3 py-1.5">
          <span className="font-mono font-semibold">v{version.revision}</span>
          <span className="flex-1 truncate">{version.note || ''}</span>
          <span className="text-muted whitespace-nowrap">{formatDateTime(version.publishedAt)}</span>
        </div>
      ))}
    </div>
  );
}







// -----------------------------------------------------------
// BuildingDetails (default export)
// -----------------------------------------------------------
//
// The parent mounts/unmounts this component instead of
// toggling `open`.
//
// Used by:
//   - BuildingsTable — opened on row click
// -----------------------------------------------------------

export default function BuildingDetails({ row, authData, onClose }) {

  const t = useTranslations("PAGES.wayfind");
  const queryClient = useQueryClient();
  const isAdmin = authData?.role === 'admin';

  // The last refused publish's issue list — kept on screen
  // until the next attempt
  const [issues, setIssues] = useState(null);


  // The version history (admin/curator read)
  const { data: versions } = useQuery({
    queryKey: ['wayfind', 'versions', row.id],
    queryFn: async () => (await api.get(`/api/wayfind/buildings/${row.id}/versions`)).data,
  });


  const publish = useMutation({
    mutationFn: async () => (await api.post(`/api/wayfind/buildings/${row.id}/publish`, {})).data,
    onSuccess: (published) => {
      setIssues(null);
      toast.success(<b>{t("PUBLISH.done_toast", { revision: published.revision })}</b>, { duration: 4000 });
      queryClient.invalidateQueries({ queryKey: ['wayfind'] });
      onClose();
    },
    onError: (error) => {
      // The 422 carries the checker's findings — show them in
      // place (the generic toast already fired in the client)
      setIssues(error.response?.data?.issues ?? null);
    },
  });


  const pending = row.draftRevision > (row.publishedRevision ?? 0);
  const graphUrl = `/api/wayfind/buildings/${row.id}/graph`;


  return (
    <UniversalModal
      open={true}   // always open — the parent mounts/unmounts this component instead
      onClose={onClose}
      title={row.name}
      description={row.id}
      maxWidth={480}
      fullWidth
      actions={isAdmin ? (
        <Button
          variant="contained"
          fullWidth
          disabled={publish.isPending || !pending}
          onClick={() => publish.mutate()}
        >
          {pending ? t("PUBLISH.button") : t("PUBLISH.nothing")}
        </Button>
      ) : undefined}
      showCancel={!isAdmin}
      showConfirm={false}
    >
      <div className="flex flex-col gap-2">

        <FactRow label={t("COLUMNS.draft")}>v{row.draftRevision}</FactRow>
        <FactRow label={t("COLUMNS.published")}>
          {row.publishedRevision != null ? `v${row.publishedRevision}` : t("STATE.unpublished")}
        </FactRow>
        {row.publishedAt && (
          <FactRow label={t("COLUMNS.publishedAt")}>{formatDateTime(row.publishedAt)}</FactRow>
        )}
        {row.northDeg != null && (
          <FactRow label={t("FIELDS.north")}>{row.northDeg}°</FactRow>
        )}

        {/* The live document the phones download */}
        {row.publishedRevision != null && (
          <a href={graphUrl} target="_blank" rel="noopener noreferrer"
             className="text-primary text-sm underline flex items-center gap-1">
            {t("open_graph")} <OpenInNewIcon sx={{ fontSize: 16 }} />
          </a>
        )}

        {/* Why the last publish was refused, when it was */}
        <IssueList issues={issues} t={t} />

        {/* Version history */}
        <div className="text-sm font-semibold mt-2">{t("VERSIONS.title")}</div>
        <VersionHistory versions={versions?.versions} t={t} />

      </div>
    </UniversalModal>
  );
}
