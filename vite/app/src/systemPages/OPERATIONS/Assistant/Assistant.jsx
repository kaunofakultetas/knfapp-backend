// -----------------------------------------------------------
//  [*] OPERATIONS — AI assistant
//
//  The assistant's maintenance console (admin only): the
//  week's turn and thumbs numbers, the knowledge base per
//  source with the on-demand re-index (cron's exact sync —
//  incremental is subsecond, the full re-embed asks first),
//  a retrieval test box showing precisely what the agent's
//  searchHandbook tool would be handed for a question, the
//  CURATED ANSWERS an admin writes by hand (embedded on save,
//  live for the next chat turn, edited or deleted in place),
//  and the versioned prompt manager — the assistant's ENTIRE
//  system prompt lives here as immutable versions, one
//  active at most; with none active the assistant refuses
//  every chat (503) until an admin activates one. The
//  conversation review lives on its own page
//  (/assistant-threads).
//
//  Reads GET /api/admin/assistant/overview, .../prompts and
//  .../knowledge/curated (TanStack queries under the
//  ['assistant', …] prefix); every mutation invalidates the
//  prefix so the numbers, the entries and the version list
//  never disagree.
//
//  Split into (root component last):
//
//    StatTile        — one overview number
//    KnowledgeCard   — sources, freshness, re-index
//    RetrievalTester — question in, the tool's hits out
//    CuratedCard     — the hand-written answers, editable
//    PromptComposer  — a NEW version, save or save+activate
//    PromptRow       — one immutable version, expandable
//    Assistant       — the page itself (default export)
// -----------------------------------------------------------

import { useState } from "react";
import { Button, CircularProgress, MenuItem, TextField } from '@mui/material';
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
// pushes the whole corpus through the AI gateway. A run that
// would retire over a quarter of the corpus retires nothing
// (retireBlocked — usually a broken scrape); the page then
// asks whether the shrink is real before re-running with
// allowMassRetire.
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
          onClick={() => reindex.mutate({ all: false })}
        >
          {t("knowledge.reindex")}
        </Button>
        <Button
          variant="outlined"
          size="small"
          color="warning"
          disabled={reindex.isPending}
          onClick={() => {
            if (window.confirm(t("knowledge.reindexAllConfirm"))) reindex.mutate({ all: true });
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
// CuratedCard
// -----------------------------------------------------------
//
// The answers no scraped page carries, written by hand: a
// language, a one-line question (the line the assistant
// cites) and the answer. Saving embeds the entry through
// the AI gateway at once — it is live for the next chat
// turn, no re-index needed. The same form edits an existing
// entry (prefilled; a save re-embeds it), and every entry
// carries its delete. The repo-owned base entries are code
// and are not listed here.
//
// Used by:
//   - Assistant (below)
// -----------------------------------------------------------

function CuratedCard({ t, entries, save, remove }) {

  const [editing, setEditing] = useState(null);
  const [language, setLanguage] = useState('lt');
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');


  const reset = () => {
    setEditing(null);
    setQuestion('');
    setAnswer('');
  };

  const startEdit = (entry) => {
    setEditing(entry);
    setLanguage(entry.language);
    setQuestion(entry.question);
    setAnswer(entry.answer);
  };

  const submit = () => {
    save.mutate(
      { id: editing?.id ?? null, language, question: question.trim(), answer: answer.trim() },
      { onSuccess: reset },
    );
  };


  return (
    <div className="rounded-xl border border-gray-200 bg-white p-4">
      <div className="mb-1 font-semibold text-gray-900">
        {t("curated.title")} — {entries.length}
      </div>
      <div className="mb-3 text-sm text-gray-500">{t("curated.hint")}</div>

      {/* The form — adds, or edits the entry it was opened on */}
      <div className="flex flex-col gap-2">
        <div className="flex gap-2">
          <TextField
            select
            size="small"
            value={language}
            onChange={(event) => setLanguage(event.target.value)}
            sx={{ width: 96 }}
          >
            <MenuItem value="lt">LT</MenuItem>
            <MenuItem value="en">EN</MenuItem>
          </TextField>
          <TextField
            size="small"
            fullWidth
            placeholder={t("curated.questionPlaceholder")}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
          />
        </div>
        <TextField
          size="small"
          fullWidth
          multiline
          minRows={3}
          placeholder={t("curated.answerPlaceholder")}
          value={answer}
          onChange={(event) => setAnswer(event.target.value)}
        />
        <div className="flex gap-2">
          <Button
            variant="contained"
            size="small"
            disabled={!question.trim() || !answer.trim() || save.isPending}
            startIcon={save.isPending ? <CircularProgress size={14} color="inherit" /> : null}
            onClick={submit}
          >
            {editing ? t("curated.update") : t("curated.add")}
          </Button>
          {editing ? (
            <Button variant="outlined" size="small" onClick={reset}>
              {t("curated.cancel")}
            </Button>
          ) : null}
        </div>
      </div>

      {/* The entries, newest first */}
      {entries.length === 0 ? (
        <div className="mt-3 text-sm text-gray-500">{t("curated.empty")}</div>
      ) : null}
      {entries.map((entry) => (
        <div key={entry.id} className="mt-3 border-t border-gray-100 pt-2">
          <div className="flex items-center gap-2">
            <span className="text-xs uppercase text-gray-400">{entry.language}</span>
            <span className="text-sm font-medium text-gray-900">{entry.question}</span>
            <span className="ml-auto text-xs text-gray-400">{formatDateTime(entry.indexedAt)}</span>
          </div>
          <div className="whitespace-pre-wrap text-sm text-gray-600">{entry.answer}</div>
          <div className="mt-1 flex gap-2">
            <Button size="small" onClick={() => startEdit(entry)}>
              {t("curated.edit")}
            </Button>
            <Button
              size="small"
              color="error"
              disabled={remove.isPending}
              onClick={() => {
                if (window.confirm(t("curated.deleteConfirm"))) remove.mutate(entry.id);
              }}
            >
              {t("curated.delete")}
            </Button>
          </div>
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
// The three queries load side by side; the six mutations
// (re-index — re-offered with allowMassRetire when the share
// guard blocked a retirement —, save/delete a curated
// answer, save version, activate, core-only) each
// invalidate the prefix on success and toast their outcome —
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
  const curated = useQuery({
    queryKey: ['assistant', 'curated'],
    queryFn: async () => (await api.get('/api/admin/assistant/knowledge/curated')).data.entries,
  });

  const refetchBoth = () => {
    queryClient.invalidateQueries({ queryKey: ['assistant'] });
  };

  const reindex = useMutation({
    mutationFn: async ({ all = false, allowMassRetire = false }) =>
      (await api.post('/api/admin/assistant/knowledge/reindex', { all, allowMassRetire })).data,
    onSuccess: (counts, variables) => {
      toast.success(t("knowledge.reindexDone")
        .replace('{embedded}', counts.embedded)
        .replace('{unchanged}', counts.unchanged)
        .replace('{retired}', counts.retired));
      refetchBoth();
      // The share guard held a mass retirement back — only an
      // admin who knows the source really shrank lets it through
      if (counts.retireBlocked > 0
          && window.confirm(t("knowledge.retireBlockedConfirm").replace('{count}', counts.retireBlocked))) {
        reindex.mutate({ all: variables.all, allowMassRetire: true });
      }
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
  const saveCurated = useMutation({
    mutationFn: async ({ id, ...body }) =>
      (await api.post(id
        ? `/api/admin/assistant/knowledge/curated/${id}/update`
        : '/api/admin/assistant/knowledge/curated', body)).data,
    onSuccess: () => {
      toast.success(t("curated.saved"));
      refetchBoth();
    },
  });
  const removeCurated = useMutation({
    mutationFn: async (id) => (await api.post(`/api/admin/assistant/knowledge/curated/${id}/delete`)).data,
    onSuccess: refetchBoth,
  });


  return (
    <PageLayout authData={authData}>
      <div className="h-full overflow-y-auto p-5">

        <PageTitle>{t("TITLE")}</PageTitle>

        {overview.isLoading || prompts.isLoading || curated.isLoading ? (
          <div className="flex justify-center py-10"><CircularProgress /></div>
        ) : overview.isError || prompts.isError || curated.isError ? (
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
            <CuratedCard t={t} entries={curated.data ?? []} save={saveCurated} remove={removeCurated} />

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
