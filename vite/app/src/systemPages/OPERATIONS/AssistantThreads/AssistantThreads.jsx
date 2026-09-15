// -----------------------------------------------------------
//  [*] OPERATIONS — AI conversations
//
//  The assistant's conversation review (admin only): every
//  stored thread in a DataGrid — title, the newest answer's
//  preview, who asked (or guest), message count, the thumbs
//  verdicts, a deleted flag for threads the student swiped
//  (they stay reviewable until cron prunes them) — with a
//  complaints-only toggle narrowing to conversations that
//  hold a thumbs-down. A row opens the transcript dialog;
//  every transcript fetch writes an admin_audit row
//  server-side, because student chats are personal data.
//
//  Reads GET /api/admin/assistant/threads (TanStack query
//  ['assistant','threads', onlyDown]) and the transcript on
//  demand (['assistant','thread', id]).
//
//  Split into (root component last):
//
//    Columns          — the grid column set built from t
//    TranscriptDialog — the read-only exchange
//    AssistantThreads — the page itself (default export)
// -----------------------------------------------------------

import { useMemo, useState } from "react";
import { DataGrid } from "@mui/x-data-grid";
import { Chip, CircularProgress, FormControlLabel, Switch } from '@mui/material';
import { useQuery } from '@tanstack/react-query';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { formatDateTime, parseTimestamp } from '@/utils/timestamps';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import UniversalModal from '@/components/UniversalModal/UniversalModal';







// -----------------------------------------------------------
// Columns
// -----------------------------------------------------------
//
// The review grid's columns. The thumbs cells render as
// pills only when non-zero, so the grid reads as a triage
// list — the eye lands on the red ones.
//
// Used by:
//   - AssistantThreads (below)
// -----------------------------------------------------------

const Columns = (t) => [
  { field: 'title', headerName: t("columns.title"), flex: 1.4, minWidth: 200,
    valueGetter: (value) => value || t("untitled") },
  { field: 'preview', headerName: t("columns.preview"), flex: 2, minWidth: 240 },
  { field: 'user', headerName: t("columns.user"), width: 130,
    valueGetter: (value) => value || t("guest") },
  { field: 'messages', headerName: t("columns.messages"), width: 90, type: 'number' },
  { field: 'up', headerName: '👍', width: 70, type: 'number',
    renderCell: ({ value }) => (value > 0 ? <Chip size="small" color="success" label={value} /> : null) },
  { field: 'down', headerName: '👎', width: 70, type: 'number',
    renderCell: ({ value }) => (value > 0 ? <Chip size="small" color="error" label={value} /> : null) },
  { field: 'deleted', headerName: t("columns.deleted"), width: 100,
    renderCell: ({ value }) => (value ? <Chip size="small" color="warning" label={t("deletedBadge")} /> : null) },
  { field: 'lastMessageAt', headerName: t("columns.lastMessageAt"), width: 150,
    valueGetter: (value) => parseTimestamp(value), valueFormatter: (value) => formatDateTime(value),
    type: 'dateTime' },
];







// -----------------------------------------------------------
// TranscriptDialog
// -----------------------------------------------------------
//
// The read-only exchange: the student right in brand, the
// agent left on gray, tool names and the thumbs verdict
// under an answer when either exists. Fetches only while
// open — the fetch itself is what the server audits.
//
// Used by:
//   - AssistantThreads (below)
// -----------------------------------------------------------

function TranscriptDialog({ t, threadId, onClose }) {

  const transcript = useQuery({
    queryKey: ['assistant', 'thread', threadId],
    queryFn: async () => (await api.get(`/api/admin/assistant/threads/${threadId}`)).data,
    enabled: threadId !== null,
  });


  const data = transcript.data;
  return (
    <UniversalModal
      open={threadId !== null}
      onClose={onClose}
      title={data ? (data.title || t("untitled")) : t("transcript.title")}
      description={data
        ? `${data.user || t("guest")} · ${data.language.toUpperCase()} · ${formatDateTime(data.lastMessageAt)}`
        : ''}
      showConfirm={false}
      cancelText={t("transcript.close")}
    >
      {transcript.isLoading ? (
        <div className="flex justify-center py-6"><CircularProgress size={22} /></div>
      ) : transcript.isError ? (
        <div className="py-4 text-sm text-red-600">{t("loadError")}</div>
      ) : (
        <div className="flex max-h-[60vh] flex-col gap-2 overflow-y-auto pr-1">
          {(data?.messages ?? []).map((message) => (
            <div key={message.id}
                 className={message.role === 'user' ? 'flex justify-end' : 'flex justify-start'}>
              <div className={message.role === 'user'
                ? 'max-w-[85%] rounded-2xl rounded-br-md bg-red-900 px-3 py-2 text-sm text-white'
                : 'max-w-[85%] rounded-2xl rounded-bl-md bg-gray-100 px-3 py-2 text-sm text-gray-900'}>
                <div className="whitespace-pre-wrap">{message.text || '—'}</div>
                {message.role !== 'user' && (message.tools.length > 0 || message.rating !== null) ? (
                  <div className="mt-1 text-xs opacity-70">
                    {message.tools.join(', ')}
                    {message.rating === 1 ? ' 👍' : message.rating === -1 ? ' 👎' : ''}
                  </div>
                ) : null}
              </div>
            </div>
          ))}
        </div>
      )}
    </UniversalModal>
  );
}







// -----------------------------------------------------------
// AssistantThreads (default export)
// -----------------------------------------------------------
//
// The complaints toggle re-keys the list query; a row click
// opens the transcript dialog with that thread. First page
// of two hundred only — pagination waits for the day the
// faculty outgrows it.
//
// Used by:
//   - router.jsx — route /assistant-threads (via PageWrapper)
// -----------------------------------------------------------

export default function AssistantThreads({ authData }) {

  const t = useTranslations("PAGES.assistantThreads");
  const [onlyDown, setOnlyDown] = useState(false);
  const [openId, setOpenId] = useState(null);


  const threads = useQuery({
    queryKey: ['assistant', 'threads', onlyDown],
    queryFn: async () =>
      (await api.get(`/api/admin/assistant/threads?per_page=50${onlyDown ? '&rating=down' : ''}`)).data,
  });


  const columns = useMemo(() => Columns(t), [t]);
  return (
    <PageLayout authData={authData}>
      <div className="h-full p-5 flex flex-col">

        <PageTitle>{t("TITLE")}</PageTitle>

        <FormControlLabel
          control={<Switch checked={onlyDown} onChange={(event) => setOnlyDown(event.target.checked)} />}
          label={t("onlyDown")}
        />

        {/* The review grid fills the rest of the viewport */}
        <div className="flex-1 min-h-0">
          <DataGrid
            rows={threads.data?.threads ?? []}
            columns={columns}
            loading={threads.isLoading}
            disableRowSelectionOnClick
            onRowClick={({ row }) => setOpenId(row.id)}
            initialState={{ sorting: { sortModel: [{ field: 'lastMessageAt', sort: 'desc' }] } }}
          />
        </div>

        <TranscriptDialog t={t} threadId={openId} onClose={() => setOpenId(null)} />

      </div>
    </PageLayout>
  );
}
