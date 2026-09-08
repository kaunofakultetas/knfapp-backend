// -----------------------------------------------------------
//  [*] HOME — Home (dashboard page)
//
//  The landing page of the panel: counter widgets over the
//  whole system plus a scraper health strip. Data comes from
//  three sources, each with its own cadence while the page
//  is on screen:
//    - /api/admin/stats    — 15 s (admin only; the backend
//      caches the counters for 45 s anyway)
//    - /api/admin/reports?status=open — 30 s (admin and
//      curator; the widget shows the open moderation queue)
//    - /api/scraper/status — 30 s (admin only)
//
//  Curators land here too but only see what their role can
//  read: the moderation queue and their own invitations —
//  the other queries are never sent (they would 403).
//
//  Split into (root component last):
//
//    WidgetIcon    — the pink rounded corner icon
//    ScraperStrip  — per-source health pills + last runs
//    Home          — the dashboard itself (default export)
// -----------------------------------------------------------

import { Link } from "react-router-dom";
import { useQuery } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';
import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { formatDateTime } from '@/utils/timestamps';

import PageLayout from "@/systemPages/PageLayout";
import Widget from "@/components/widget/Widget";

// Widget corner icons
import PersonOutlineIcon from '@mui/icons-material/PersonOutline';
import ArticleOutlinedIcon from '@mui/icons-material/ArticleOutlined';
import CommentOutlinedIcon from '@mui/icons-material/CommentOutlined';
import VpnKeyOutlinedIcon from '@mui/icons-material/VpnKeyOutlined';
import FlagOutlinedIcon from '@mui/icons-material/FlagOutlined';
import CloudSyncOutlinedIcon from '@mui/icons-material/CloudSyncOutlined';


// Scraper run status → the house pill colors: green =
// finished clean, orange = still going, red = failed
const RUN_COLORS = {
  completed: 'green',
  running: 'orange',
  failed: 'red',
};







// -----------------------------------------------------------
// WidgetIcon
// -----------------------------------------------------------
//
// The pink rounded square holding a white icon in the
// widget's corner.
//
// Used by:
//   - Home (below) — one per counter widget
// -----------------------------------------------------------

function WidgetIcon({ icon: Icon }) {
  return (
    <div className="flex items-center justify-center w-9 h-9 rounded-lg bg-primary-dark">
      <Icon sx={{ color: 'white', fontSize: 22 }} />
    </div>
  );
}







// -----------------------------------------------------------
// ScraperStrip
// -----------------------------------------------------------
//
// One white card summarising every scraper source: the
// source name, its latest run as a colored pill, and when it
// last succeeded. The whole card links to the scrapers page
// where runs can be inspected and triggered.
//
// Used by:
//   - Home (below) — admin only
// -----------------------------------------------------------

function ScraperStrip({ sources, t }) {
  return (
    <div className="rounded-[15px] bg-white shadow-card p-4 mt-4">

      <Link to="/scrapers" className="no-underline">
        <div className="flex items-center gap-2 mb-3 text-gray-500">
          <CloudSyncOutlinedIcon sx={{ color: 'primary.main' }} />
          <span className="font-bold text-sm">{t("SCRAPERS.title")}</span>
        </div>
      </Link>

      <div className="flex flex-wrap gap-3">
        {sources.map((entry) => (
          <div key={entry.source} className="flex-1 min-w-[220px] rounded-[12px] border border-edge p-3">
            <div className="text-[13px] font-semibold text-[rgb(65,65,65)] mb-2">{entry.source}</div>

            {/* Latest run status pill */}
            <div
              className="inline-block px-2 rounded-md text-white text-xs text-center min-w-[90px]"
              style={{ backgroundColor: RUN_COLORS[entry.latest?.status] ?? 'grey' }}
            >
              {entry.latest ? t(`SCRAPERS.STATUS.${entry.latest.status}`) : t("SCRAPERS.never")}
            </div>

            <div className="text-xs text-muted mt-2">
              {t("SCRAPERS.last_success")}: {entry.lastSuccess ? formatDateTime(entry.lastSuccess.startedAt) : "—"}
            </div>
          </div>
        ))}
      </div>

    </div>
  );
}







// -----------------------------------------------------------
// Home (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — the index route "/" (via PageWrapper)
// -----------------------------------------------------------

export default function Home({ authData }) {

  const t = useTranslations("PAGES.home");
  const isAdmin = authData?.role === 'admin';

  // All three reads pause while the page is parked in the
  // KeepAlive cache — a hidden dashboard must not keep
  // polling
  const { active } = useKeepAliveContext();


  // System counters — admin only (curators would get a 403)
  const { data: stats = {} } = useQuery({
    queryKey: ['admin', 'stats'],
    queryFn: async () => (await api.get('/api/admin/stats')).data,
    enabled: active && isAdmin,
    refetchInterval: active && isAdmin ? 15000 : false,
  });


  // The open moderation queue — both panel roles may read it
  const { data: reports } = useQuery({
    queryKey: ['admin', 'reports', 'open'],
    queryFn: async () => (await api.get('/api/admin/reports', { params: { status: 'open' } })).data,
    enabled: active && Boolean(authData),
    refetchInterval: active ? 30000 : false,
  });


  // Invitations — the backend scopes the list by role itself
  // (admins see everything, curators their own)
  const { data: invitations } = useQuery({
    queryKey: ['admin', 'invitations'],
    queryFn: async () => (await api.get('/api/admin/invitations')).data,
    enabled: active && Boolean(authData),
  });
  const activeInvitations = invitations?.invitations?.filter((i) => !i.expired && !i.fullyUsed).length;


  // Scraper health — admin only
  const { data: scraper } = useQuery({
    queryKey: ['scraper', 'status'],
    queryFn: async () => (await api.get('/api/scraper/status')).data,
    enabled: active && isAdmin,
    refetchInterval: active && isAdmin ? 30000 : false,
  });


  return (
    <PageLayout authData={authData} backgroundColor="#EBECEF">
      <div className="p-5">

        {/* Counter widgets — the admin-only ones render for
            admins alone (their queries never ran for curators,
            so the cards would sit empty forever) */}
        <div className="flex flex-wrap gap-4">
          {isAdmin && (
            <Widget text={t("WIDGETS.users")} count={stats.users}
                    icon={<WidgetIcon icon={PersonOutlineIcon} />} link="/users" />
          )}
          {isAdmin && (
            <Widget text={t("WIDGETS.posts")} count={stats.posts}
                    bottomtext={t("WIDGETS.posts_scraped", { count: stats.scrapedArticles ?? "" })}
                    icon={<WidgetIcon icon={ArticleOutlinedIcon} />} link="/news" />
          )}
          {isAdmin && (
            <Widget text={t("WIDGETS.comments")} count={stats.comments}
                    icon={<WidgetIcon icon={CommentOutlinedIcon} />} link="/news" />
          )}
          <Widget text={t("WIDGETS.invitations")} count={activeInvitations}
                  icon={<WidgetIcon icon={VpnKeyOutlinedIcon} />} link="/invitations" />
          <Widget text={t("WIDGETS.reports")} count={reports?.reports?.length}
                  icon={<WidgetIcon icon={FlagOutlinedIcon} />} link="/reports" />
        </div>

        {/* Scraper health — admin only */}
        {isAdmin && scraper?.sources?.length > 0 && (
          <ScraperStrip sources={scraper.sources} t={t} />
        )}

      </div>
    </PageLayout>
  );
}
