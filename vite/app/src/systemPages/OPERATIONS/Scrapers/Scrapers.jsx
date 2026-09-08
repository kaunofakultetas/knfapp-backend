// -----------------------------------------------------------
//  [*] OPERATIONS — Scrapers
//
//  The scraper control room (admin only): trigger any of the
//  three scrapers by hand, watch the run history, and see
//  the news yield over time.
//
//  Reads GET /api/scraper/status (TanStack query
//  ['scraper','status'], re-polled every 5 seconds while the
//  page is on screen — a run triggered from here or by cron
//  shows up as it happens). The triggers are SYNCHRONOUS on
//  the backend — a scrape can take tens of seconds, so the
//  buttons stay disabled until the response lands. A 409
//  means that scraper is already running (the cron beat this
//  click); a 502 means the run failed and the runs grid
//  carries the error text.
//
//  Split into (root component last):
//
//    RUN_COLORS      — run status → pill color
//    TriggerBar      — the three trigger buttons
//    YieldChart      — new articles per news run (area chart)
//    Runs_Columns    — runs grid column set built from t
//    Scrapers        — the page itself (default export)
// -----------------------------------------------------------

import { useMemo } from "react";
import { DataGrid, GridLogicOperator } from "@mui/x-data-grid";
import { LinearProgress, CircularProgress } from '@mui/material';
import { AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';
import toast from 'react-hot-toast';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { dateTimeColumn, formatDateTime, parseTimestamp } from '@/utils/timestamps';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import ToolbarButton from '@/components/DatagridCustomComponents/ToolbarButton';
import QuickSearchToolbar from '@/components/DatagridCustomComponents/QuickSearchToolbar';
import CustomPagination from '@/components/other/ButtonsPagination/ButtonsPagination';

import NewspaperOutlinedIcon from '@mui/icons-material/NewspaperOutlined';
import CalendarMonthOutlinedIcon from '@mui/icons-material/CalendarMonthOutlined';
import MenuBookOutlinedIcon from '@mui/icons-material/MenuBookOutlined';


// Run status → the house pill colors
const RUN_COLORS = {
  completed: 'green',
  running: 'orange',
  failed: 'red',
};


// The news sources — the yield chart only plots these (the
// timetable and info runs count different things)
const NEWS_SOURCES = ['knf.vu.lt', 'vu.lt'];







// -----------------------------------------------------------
// TriggerBar
// -----------------------------------------------------------
//
// The three manual triggers. One shared spinner rule: while
// ANY trigger is in flight all three are disabled — the
// backend serialises runs per source anyway, and a second
// click would just collect a 409.
//
// Used by:
//   - Scrapers (below)
// -----------------------------------------------------------

function TriggerBar({ onTrigger, busy, t }) {
  return (
    <div className="flex items-center gap-1 flex-wrap">
      <ToolbarButton label={t("TRIGGERS.news")} icon={NewspaperOutlinedIcon}
                     disabled={busy} onClick={() => onTrigger('trigger')} />
      <ToolbarButton label={t("TRIGGERS.schedule")} icon={CalendarMonthOutlinedIcon}
                     disabled={busy} onClick={() => onTrigger('schedule')} />
      <ToolbarButton label={t("TRIGGERS.info")} icon={MenuBookOutlinedIcon}
                     disabled={busy} onClick={() => onTrigger('info')} />
      {busy && <CircularProgress size={20} sx={{ ml: 1 }} />}
    </div>
  );
}







// -----------------------------------------------------------
// YieldChart
// -----------------------------------------------------------
//
// New articles per completed news run, oldest to newest —
// the quickest way to see whether the site scrapers still
// find anything. Hidden entirely while there are no
// completed news runs to plot.
//
// Used by:
//   - Scrapers (below)
// -----------------------------------------------------------

function YieldChart({ runs, t }) {

  // Completed news runs, oldest first; the label drops the
  // year ("09-08 14:30") to keep the axis readable
  const data = runs
    .filter((run) => NEWS_SOURCES.includes(run.source) && run.status === 'completed')
    .slice()
    .sort((a, b) => (parseTimestamp(a.startedAt) ?? 0) - (parseTimestamp(b.startedAt) ?? 0))
    .map((run) => ({
      name: formatDateTime(run.startedAt).slice(5),
      source: run.source,
      [t("CHART.new_articles")]: run.articlesNew,
    }));

  if (data.length === 0) return null;

  return (
    <div className="rounded-[15px] bg-white shadow-card p-4 mb-4" style={{ height: 220 }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
          <defs>
            <linearGradient id="yield" x1="0" y1="0" x2="0" y2="1">
              <stop offset="5%" stopColor="#7B003F" stopOpacity={0.8} />
              <stop offset="95%" stopColor="#7B003F" stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="name" stroke="gray" tickCount={5} />
          <YAxis type="number" allowDecimals={false} />
          <CartesianGrid strokeDasharray="3 3" style={{ stroke: 'rgb(228, 225, 225)' }} />
          <Tooltip />
          <Area type="monotone" dataKey={t("CHART.new_articles")} stroke="#7B003F"
                fillOpacity={1} fill="url(#yield)" />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}







// -----------------------------------------------------------
// Runs_Columns
// -----------------------------------------------------------
//
// The runs grid column set. A function (not a const) because
// the headers and pill texts need t(); memoized by the
// caller.
//
// Used by:
//   - Scrapers (below)
// -----------------------------------------------------------

function Runs_Columns(t) {
  return [
    {
      field: "startedAt",
      headerName: t("COLUMNS.startedAt"),
      width: 160,
      ...dateTimeColumn,
    },
    {
      field: "source",
      headerName: t("COLUMNS.source"),
      width: 170,
    },
    {
      field: "status",
      headerName: t("COLUMNS.status"),
      width: 120,
      renderCell: (params) => (
        <div
          className="px-1.5 rounded-md w-[100px] text-center text-white"
          style={{ backgroundColor: RUN_COLORS[params.value] ?? 'grey' }}
        >
          {t(`STATUS.${params.value}`)}
        </div>
      ),
    },
    {
      field: "itemsFound",
      headerName: t("COLUMNS.found"),
      width: 100,
      type: "number",
    },
    {
      field: "itemsNew",
      headerName: t("COLUMNS.new"),
      width: 100,
      type: "number",
    },
    {
      field: "finishedAt",
      headerName: t("COLUMNS.finishedAt"),
      width: 160,
      ...dateTimeColumn,
    },
    {
      field: "error",
      headerName: t("COLUMNS.error"),
      flex: 1,
      minWidth: 200,
    },
  ];
}







// -----------------------------------------------------------
// Scrapers (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /scrapers (via PageWrapper);
//     admin only (the sidebar hides it from curators)
// -----------------------------------------------------------

export default function Scrapers({ authData }) {

  const t = useTranslations("PAGES.scrapers");
  const queryClient = useQueryClient();

  const { active } = useKeepAliveContext();
  const { data = {}, isLoading: loadingData } = useQuery({
    queryKey: ['scraper', 'status'],
    queryFn: async () => (await api.get('/api/scraper/status')).data,
    enabled: active,
    refetchInterval: active ? 5000 : false,
  });


  // One mutation for all three triggers — the endpoint tail
  // picks the scraper. Errors (409 already running, 502 run
  // failed) are toasted by the api client; either way the
  // runs grid tells the full story on the next poll.
  const trigger = useMutation({
    mutationFn: async (endpoint) => (await api.post(`/api/scraper/${endpoint}`)).data,
    onSuccess: () => {
      toast.success(<b>{t("trigger_done_toast")}</b>, { duration: 3000 });
    },
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['scraper', 'status'] });
    },
  });

  const columns = useMemo(() => Runs_Columns(t), [t]);


  return (
    <PageLayout authData={authData}>
      <div className="h-full p-5 flex flex-col">

        <PageTitle>
          {t("TITLE")}
          <TriggerBar onTrigger={(endpoint) => trigger.mutate(endpoint)}
                      busy={trigger.isPending} t={t} />
        </PageTitle>

        {/* News yield over the kept runs */}
        <YieldChart runs={data.runs ?? []} t={t} />

        {/* The run history (the backend keeps ~30 days,
            answers the 20 newest) */}
        <div className="flex-1 min-h-[300px] rounded-[15px] bg-white p-4 shadow-card">
          <DataGrid
            sx={{ height: '100%', border: 'none' }}
            rows={data.runs ?? []}
            columns={columns}
            rowHeight={30}
            pageSizeOptions={[100]}
            showToolbar
            loading={loadingData}

            initialState={{
              filter: {
                filterModel: {
                  items: [],
                  quickFilterLogicOperator: GridLogicOperator.Or,
                  quickFilterExcludeHiddenColumns: false,
                },
              },
              pagination: {
                paginationModel: { pageSize: 100 },
              },
            }}

            slots={{
              toolbar: QuickSearchToolbar,
              loadingOverlay: LinearProgress,
              pagination: CustomPagination,
            }}

            slotProps={{
              panel: { placement: 'bottom-start' },
            }}
          />
        </div>

      </div>
    </PageLayout>
  );
}
