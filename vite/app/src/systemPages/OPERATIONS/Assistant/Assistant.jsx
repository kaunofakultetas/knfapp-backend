// -----------------------------------------------------------
//  [*] OPERATIONS — AI assistant
//
//  The assistant's maintenance console (admin only): the
//  week's turn and thumbs numbers, the knowledge base per
//  source with the on-demand re-index (cron's exact sync —
//  incremental is subsecond, the full re-embed asks first),
//  a retrieval test box showing precisely what the agent's
//  searchHandbook tool would be handed for a question, and
//  the versioned prompt manager — the assistant's ENTIRE
//  system prompt lives here as immutable versions, one
//  active at most; with none active the assistant refuses
//  every chat (503) until an admin activates one. The
//  conversation review lives on its own page
//  (/assistant-threads).
//
//  Reads GET /api/admin/assistant/overview and .../prompts
//  (TanStack queries ['assistant','overview'] and
//  ['assistant','prompts']); every mutation invalidates
//  both so the numbers and the version list never disagree.
//
//  Split into (root component last):
//
//    StatTile        — one overview number
//    KnowledgeCard   — sources, freshness, re-index
//    RetrievalTester — question in, the tool's hits out
//    PromptComposer  — a NEW version, save or save+activate
//    PromptRow       — one immutable version, expandable
//    Assistant       — the page itself (default export)
// -----------------------------------------------------------

import { useState } from "react";
import { Button, CircularProgress, TextField } from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import toast from 'react-hot-toast';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { formatDateTime } from '@/utils/timestamps';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";

import RefreshOutlinedIcon from '@mui/icons-material/RefreshOutlined';
import SearchOutlinedIcon from '@mui/icons-material/SearchOutlined';







// -----------------------------------------------------------
// StatTile
// -----------------------------------------------------------
//
// One overview number in the house card look — label over
// the count, red when the count is a bad sign and non-zero.
//
// Used by:
//   - Assistant (below) — the overview row
// -----------------------------------------------------------

function StatTile({ label, value, bad = false }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white px-4 py-3">
      <div className="text-xs text-gray-500">{label}</div>
      <div className={`text-xl font-semibold ${bad && value > 0 ? 'text-red-600' : 'text-gray-900'}`}>
        {value}
      </div>
    </div>
  );
}







// -----------------------------------------------------------
// KnowledgeCard
// -----------------------------------------------------------
//
// The knowledge base per source with its newest indexing
// stamp, and the two sync buttons. Both run cron's exact
// sync synchronously — the buttons stay disabled until the
// counts land; the full re-embed confirms first because it
// pushes the whole corpus through the AI gateway.
//
// Used by:
//   - Assistant (below)
// -----------------------------------------------------------

function KnowledgeCard({ t, knowledge, reindex }) {
  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4">
      <div className="mb-2 font-semibold text-gray-900">
        {t("knowledge.title")} — {knowledge.total}
      </div>
      <table className="w-full text-sm">
        <tbody>
          {knowledge.sources.map((row) => (
            <tr key={row.source} className="border-t border-gray-100">
              <td className="py-1 text-gray-700">{row.source}</td>
              <td className="py-1 text-right text-gray-900">{row.count}</td>
              <td className="py-1 text-right text-gray-500">
                {row.indexedAt ? formatDateTime(row.indexedAt) : '—'}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="mt-3 flex gap-2">
        <Button
          variant="contained"
          size="small"
          startIcon={reindex.isPending ? <CircularProgress size={14} color="inherit" /> : <RefreshOutlinedIcon />}
          disabled={reindex.isPending}
          onClick={() => reindex.mutate(false)}
        >
          {t("knowledge.reindex")}
        </Button>
        <Button
          variant="outlined"
          size="small"
          color="warning"
          disabled={reindex.isPending}
          onClick={() => {
            if (window.confirm(t("knowledge.reindexAllConfirm"))) reindex.mutate(true);
          }}
        >
          {t("knowledge.reindexAll")}
        </Button>
      </div>
    </div>
  );
}







// -----------------------------------------------------------
// RetrievalTester
// -----------------------------------------------------------
//
// The "why did it cite that" box: a student question in,
// the EXACT searchHandbook entries out, numbered the way
// the app's sources footer numbers them. An empty answer
// renders as the honest "no grounding" line.
//
// Used by:
//   - Assistant (below)
// -----------------------------------------------------------

function RetrievalTester({ t }) {

  const [query, setQuery] = useState('');
  const search = useMutation({
    mutationFn: async (q) =>
      (await api.post('/api/admin/assistant/knowledge/search', { query: q })).data.results,
  });


  const run = () => {
    if (query.trim()) search.mutate(query.trim());
  };


  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4">
      <div className="mb-2 font-semibold text-gray-900">{t("test.title")}</div>
      <div className="flex gap-2">
        <TextField
          size="small"
          fullWidth
          placeholder={t("test.placeholder")}
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') run();
          }}
        />
        <Button
          variant="contained"
          size="small"
          startIcon={search.isPending ? <CircularProgress size={14} color="inherit" /> : <SearchOutlinedIcon />}
          disabled={search.isPending}
          onClick={run}
        >
          {t("test.run")}
        </Button>
      </div>
      {search.data && search.data.length === 0 ? (
        <div className="mt-3 text-sm text-gray-500">{t("test.empty")}</div>
      ) : null}
      {(search.data ?? []).map((hit, index) => (
        <div key={hit.id} className="mt-3 border-t border-gray-100 pt-2">
          <div className="text-sm font-medium text-gray-900">
            [{index + 1}] {hit.title}
            {hit.section ? ` — ${hit.section}` : ''}
            <span className="ml-2 text-xs uppercase text-gray-400">{hit.language}</span>
          </div>
          <div className="text-sm text-gray-600">{hit.excerpt}</div>
        </div>
      ))}
    </div>
  );
}







// -----------------------------------------------------------
// PromptComposer
// -----------------------------------------------------------
//
// A NEW immutable version of the WHOLE system prompt: the
// template (placeholders legend below the box), an optional
// note on why, and two exits — save as a draft version, or
// save and flip it live in the same transaction. Editing an
// old version does not exist by design; a fix is the next
// version.
//
// Used by:
//   - Assistant (below)
// -----------------------------------------------------------

function PromptComposer({ t, create }) {

  const [text, setText] = useState('');
  const [notes, setNotes] = useState('');


  const submit = (activate) => {
    create.mutate({ text: text.trim(), notes: notes.trim() || undefined, activate });
    setText('');
    setNotes('');
  };


  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4">
      <TextField
        size="small"
        fullWidth
        multiline
        minRows={10}
        placeholder={t("prompts.placeholder")}
        value={text}
        onChange={(event) => setText(event.target.value)}
      />
      <div className="mt-1 text-xs text-gray-400">{t("prompts.legend")}</div>
      <TextField
        className="mt-2"
        size="small"
        fullWidth
        placeholder={t("prompts.notesPlaceholder")}
        value={notes}
        onChange={(event) => setNotes(event.target.value)}
      />
      <div className="mt-3 flex gap-2">
        <Button variant="contained" size="small" disabled={!text.trim() || create.isPending}
                onClick={() => submit(true)}>
          {t("prompts.saveActivate")}
        </Button>
        <Button variant="outlined" size="small" disabled={!text.trim() || create.isPending}
                onClick={() => submit(false)}>
          {t("prompts.saveOnly")}
        </Button>
      </div>
    </div>
  );
}







// -----------------------------------------------------------
// PromptRow
// -----------------------------------------------------------
//
// One immutable version: number, the active pill, note and
// stamp, the full text behind a click (the list is a
// changelog first), and the activate action on every
// inactive row.
//
// Used by:
//   - Assistant (below)
// -----------------------------------------------------------

function PromptRow({ t, prompt, activate }) {

  const [open, setOpen] = useState(false);


  return (
    <div className="rounded-xl border border-gray-200 bg-white p-3">
      <button type="button" className="flex w-full items-center gap-2 text-left" onClick={() => setOpen((was) => !was)}>
        <span className="font-semibold text-gray-900">v{prompt.version}</span>
        {prompt.active ? (
          <span className="rounded-full bg-green-100 px-2 py-0.5 text-xs font-medium text-green-700">
            {t("prompts.active")}
          </span>
        ) : null}
        {prompt.notes ? <span className="text-sm text-gray-500">{prompt.notes}</span> : null}
        <span className="ml-auto text-xs text-gray-400">{formatDateTime(prompt.createdAt)}</span>
      </button>
      {open ? (
        <pre className="mt-2 whitespace-pre-wrap border-t border-gray-100 pt-2 font-sans text-sm text-gray-800">
          {prompt.text}
        </pre>
      ) : null}
      {!prompt.active ? (
        <div className="mt-2">
          <Button variant="outlined" size="small" disabled={activate.isPending}
                  onClick={() => activate.mutate(prompt.version)}>
            {t("prompts.activate")}
          </Button>
        </div>
      ) : null}
    </div>
  );
}







// -----------------------------------------------------------
// Assistant (default export)
// -----------------------------------------------------------
//
// The two queries load side by side; the four mutations
// (re-index, save version, activate, core-only) each
// invalidate both on success and toast their outcome —
// api/client.js already toasts mutation FAILURES app-wide.
//
// Used by:
//   - router.jsx — route /assistant (via PageWrapper)
// -----------------------------------------------------------

export default function Assistant({ authData }) {

  const t = useTranslations("PAGES.assistant");
  const queryClient = useQueryClient();


  const overview = useQuery({
    queryKey: ['assistant', 'overview'],
    queryFn: async () => (await api.get('/api/admin/assistant/overview')).data,
  });
  const prompts = useQuery({
    queryKey: ['assistant', 'prompts'],
    queryFn: async () => (await api.get('/api/admin/assistant/prompts')).data.prompts,
  });

  const refetchBoth = () => {
    queryClient.invalidateQueries({ queryKey: ['assistant'] });
  };

  const reindex = useMutation({
    mutationFn: async (all) => (await api.post('/api/admin/assistant/knowledge/reindex', { all })).data,
    onSuccess: (counts) => {
      toast.success(t("knowledge.reindexDone")
        .replace('{embedded}', counts.embedded)
        .replace('{unchanged}', counts.unchanged)
        .replace('{retired}', counts.retired));
      refetchBoth();
    },
  });
  const create = useMutation({
    mutationFn: async (body) => (await api.post('/api/admin/assistant/prompts', body)).data,
    onSuccess: () => {
      toast.success(t("prompts.saved"));
      refetchBoth();
    },
  });
  const activate = useMutation({
    mutationFn: async (version) => (await api.post(`/api/admin/assistant/prompts/${version}/activate`)).data,
    onSuccess: refetchBoth,
  });
  const deactivate = useMutation({
    mutationFn: async () => (await api.post('/api/admin/assistant/prompts/deactivate')).data,
    onSuccess: refetchBoth,
  });


  return (
    <PageLayout authData={authData}>
      <div className="h-full overflow-y-auto p-5">

        <PageTitle>{t("TITLE")}</PageTitle>

        {overview.isLoading || prompts.isLoading ? (
          <div className="flex justify-center py-10"><CircularProgress /></div>
        ) : overview.isError || prompts.isError ? (
          <div className="py-10 text-center text-sm text-red-600">{t("loadError")}</div>
        ) : (
          <div className="flex flex-col gap-4">

            {/* The week at a glance */}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
              <StatTile label={t("stats.turns")} value={overview.data.turns7d.total} />
              <StatTile label={t("stats.errors")} value={overview.data.turns7d.error} bad />
              <StatTile label={t("stats.up")} value={overview.data.ratings.up} />
              <StatTile label={t("stats.down")} value={overview.data.ratings.down} bad />
              <StatTile label={t("stats.threads")} value={overview.data.threads} />
            </div>

            <KnowledgeCard t={t} knowledge={overview.data.knowledge} reindex={reindex} />
            <RetrievalTester t={t} />

            {/* The versioned prompt manager */}
            <div className="mt-2 font-semibold text-gray-900">{t("prompts.title")}</div>
            <div className="text-sm text-gray-500">
              {overview.data.activePromptVersion
                ? t("prompts.activeLine").replace('{version}', overview.data.activePromptVersion)
                : t("prompts.coreLine")}
            </div>
            <PromptComposer t={t} create={create} />
            {overview.data.activePromptVersion ? (
              <div>
                <Button variant="outlined" size="small" color="warning" disabled={deactivate.isPending}
                        onClick={() => {
                          if (window.confirm(t("prompts.coreOnlyConfirm"))) deactivate.mutate();
                        }}>
                  {t("prompts.coreOnly")}
                </Button>
              </div>
            ) : null}
            {(prompts.data ?? []).map((prompt) => (
              <PromptRow key={prompt.version} t={t} prompt={prompt} activate={activate} />
            ))}

          </div>
        )}

      </div>
    </PageLayout>
  );
}
