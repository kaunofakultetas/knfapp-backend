// -----------------------------------------------------------
//  [*] CONTENT — Schedule (timetable viewer)
//
//  Read-only window into the scraped timetable the mobile app
//  serves: pick a semester and a group, see the lessons. The
//  timetable has no write path — refreshing it is the
//  scraper's job (the Scrapers page for admins, the cron
//  schedule otherwise).
//
//  Two reads:
//    - GET /api/schedule/filters — the semester/group pairs
//      for the two selects
//    - GET /api/schedule?semester=&group=&limit=500 — the
//      lessons of the chosen pair (both empty = everything
//      the newest semester holds)
//
//  Split into (root component last):
//
//    Schedule_Columns — column set built from t
//    Schedule         — selects + grid (default export)
// -----------------------------------------------------------

import { useMemo, useState } from "react";
import { DataGrid, GridLogicOperator } from "@mui/x-data-grid";
import { LinearProgress, MenuItem, TextField } from '@mui/material';
import { useQuery } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';

import api from '@/api/client';
import { useTranslations } from '@/i18n';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import QuickSearchToolbar from '@/components/DatagridCustomComponents/QuickSearchToolbar';
import CustomPagination from '@/components/other/ButtonsPagination/ButtonsPagination';







// -----------------------------------------------------------
// Schedule_Columns
// -----------------------------------------------------------
//
// The column set. A function (not a const) because the
// headers and day names need t(); memoized by the caller.
//
// Used by:
//   - Schedule (below)
// -----------------------------------------------------------

function Schedule_Columns(t) {
  return [
    {
      field: "dayOfWeek",
      headerName: t("COLUMNS.day"),
      width: 140,
      valueFormatter: (value) => t(`DAYS.${value}`),
    },
    {
      field: "timeStart",
      headerName: t("COLUMNS.time"),
      width: 130,
      valueGetter: (value, row) => `${value} – ${row.timeEnd}`,
    },
    {
      field: "title",
      headerName: t("COLUMNS.title"),
      flex: 1,
      minWidth: 240,
    },
    {
      field: "teacher",
      headerName: t("COLUMNS.teacher"),
      width: 200,
    },
    {
      field: "room",
      headerName: t("COLUMNS.room"),
      width: 110,
    },
    {
      field: "group",
      headerName: t("COLUMNS.group"),
      width: 130,
    },
  ];
}







// -----------------------------------------------------------
// Schedule (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /schedule (via PageWrapper)
// -----------------------------------------------------------

export default function Schedule({ authData }) {

  const t = useTranslations("PAGES.schedule");

  // Empty string = "not narrowed": no semester means the
  // backend serves its newest one, no group means every group
  const [semester, setSemester] = useState('');
  const [group, setGroup] = useState('');

  const { active } = useKeepAliveContext();


  // The filter pairs for the two selects
  const { data: filters = {} } = useQuery({
    queryKey: ['schedule', 'filters'],
    queryFn: async () => (await api.get('/api/schedule/filters')).data,
    enabled: active,
  });

  // Groups narrowed to the chosen semester (or every group)
  const groupOptions = useMemo(() => {
    if (!semester) return filters.groups ?? [];
    return filters.semesterGroups?.find((s) => s.semester === semester)?.groups ?? [];
  }, [filters, semester]);


  // The lessons of the chosen pair
  const { data: lessons = {}, isLoading: loadingData } = useQuery({
    queryKey: ['schedule', 'lessons', semester, group],
    queryFn: async () => (await api.get('/api/schedule', {
      params: {
        ...(semester ? { semester } : {}),
        ...(group ? { group } : {}),
        limit: 500,
      },
    })).data,
    enabled: active,
  });

  const columns = useMemo(() => Schedule_Columns(t), [t]);


  return (
    <PageLayout authData={authData}>
      <div className="h-full p-5 flex flex-col">

        <PageTitle>{t("TITLE")}</PageTitle>

        <div className="h-full rounded-[15px] bg-white p-4 shadow-card flex flex-col min-h-0">

          {/* Semester + group narrowing */}
          <div className="flex gap-3 mb-3">
            <TextField
              select
              size="small"
              label={t("FILTERS.semester")}
              value={semester}
              onChange={(e) => { setSemester(e.target.value); setGroup(''); }}
              sx={{ minWidth: 220 }}
            >
              <MenuItem value="">{t("FILTERS.newest")}</MenuItem>
              {(filters.semesters ?? []).map((s) => (
                <MenuItem key={s} value={s}>{s}</MenuItem>
              ))}
            </TextField>

            <TextField
              select
              size="small"
              label={t("FILTERS.group")}
              value={group}
              onChange={(e) => setGroup(e.target.value)}
              sx={{ minWidth: 180 }}
            >
              <MenuItem value="">{t("FILTERS.all_groups")}</MenuItem>
              {groupOptions.map((g) => (
                <MenuItem key={g} value={g}>{g}</MenuItem>
              ))}
            </TextField>
          </div>

          {/* The lessons */}
          <div className="flex-1 min-h-0">
            <DataGrid
              sx={{ height: '100%', border: 'none' }}
              rows={lessons.lessons ?? []}
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
                sorting: {
                  sortModel: [
                    { field: 'dayOfWeek', sort: 'asc' },
                    { field: 'timeStart', sort: 'asc' },
                  ],
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

      </div>
    </PageLayout>
  );
}
