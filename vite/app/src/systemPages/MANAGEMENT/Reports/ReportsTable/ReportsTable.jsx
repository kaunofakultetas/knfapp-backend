// -----------------------------------------------------------
//  [*] MANAGEMENT — ReportsTable
//
//  The moderation queue DataGrid from
//  GET /api/admin/reports?status=... (TanStack query
//  ['admin','reports',status], re-polled every 30 seconds
//  while the page is on screen — new reports should show up
//  without a manual refresh). The backend answers the 200
//  newest rows of the chosen status.
//
//  The toolbar's IOSSwitch flips between the open queue
//  (default) and the resolved archive; clicking a row opens
//  the details dialog with the resolve/reopen action.
//
//  Split into (root component last):
//
//    TARGET_COLORS        — target type → pill color
//    ReportsTable_Columns — column set built from t
//    ReportsTable         — the grid itself (default export)
// -----------------------------------------------------------

import { useMemo, useState } from "react";
import { DataGrid, GridLogicOperator } from "@mui/x-data-grid";
import { LinearProgress, FormControlLabel } from '@mui/material';
import { useQuery } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { dateTimeColumn } from '@/utils/timestamps';

import QuickSearchToolbar from '@/components/DatagridCustomComponents/QuickSearchToolbar';
import CustomPagination from '@/components/other/ButtonsPagination/ButtonsPagination';
import IOSSwitch from '@/components/other/IOSSwitch/IOSSwitch';
import ReportDetails from '../ReportDetails/ReportDetails';


// Pill colors by what was reported: accounts are the serious
// ones, content reports stay neutral
const TARGET_COLORS = {
  user: '#7B003F',
  post: 'steelblue',
  message: 'grey',
};







// -----------------------------------------------------------
// ReportsTable_Columns
// -----------------------------------------------------------
//
// The column set. A function (not a const) because the
// headers and pill texts need t(); memoized by the caller.
//
// Used by:
//   - ReportsTable (below)
// -----------------------------------------------------------

function ReportsTable_Columns(t) {
  return [
    {
      field: "createdAt",
      headerName: t("COLUMNS.createdAt"),
      width: 160,
      ...dateTimeColumn,
    },
    {
      field: "reporterName",
      headerName: t("COLUMNS.reporter"),
      width: 160,
    },
    {
      field: "targetType",
      headerName: t("COLUMNS.targetType"),
      width: 130,
      renderCell: (params) => (
        <div
          className="px-1.5 rounded-md w-[110px] text-center text-white"
          style={{ backgroundColor: TARGET_COLORS[params.value] ?? 'grey' }}
        >
          {t(`TARGETS.${params.value}`)}
        </div>
      ),
    },
    {
      field: "targetUserName",
      headerName: t("COLUMNS.targetUser"),
      width: 150,
      valueFormatter: (value) => value ?? '',
    },
    {
      field: "reason",
      headerName: t("COLUMNS.reason"),
      flex: 1,
      minWidth: 240,
    },
  ];
}







// -----------------------------------------------------------
// ReportsTable (default export)
// -----------------------------------------------------------
//
// Used by:
//   - Reports.jsx
// -----------------------------------------------------------

export default function ReportsTable() {

  const t = useTranslations("PAGES.reports");

  // false = the open queue (default), true = the resolved
  // archive; the switch drives which list the backend sends
  const [showResolved, setShowResolved] = useState(false);
  const status = showResolved ? 'resolved' : 'open';

  // The dialog is mounted/unmounted rather than toggled
  const [openedRow, setOpenedRow] = useState(undefined);

  const { active } = useKeepAliveContext();
  const { data = {}, isLoading: loadingData } = useQuery({
    queryKey: ['admin', 'reports', status],
    queryFn: async () => (await api.get('/api/admin/reports', { params: { status } })).data,
    enabled: active,
    refetchInterval: active ? 30000 : false,
  });

  const columns = useMemo(() => ReportsTable_Columns(t), [t]);


  return (
    <div className="h-full rounded-[15px] bg-white p-4 shadow-card">

      <DataGrid
        sx={{
          height: '100%',
          cursor: 'pointer',
          border: 'none',
          '& .MuiDataGrid-row:hover': {
            backgroundColor: 'rgba(123, 0, 63, 0.08)',
          },
        }}
        rows={data.reports ?? []}
        columns={columns}
        rowHeight={30}
        pageSizeOptions={[100]}
        showToolbar
        loading={loadingData}
        onRowClick={(params) => setOpenedRow(params.row)}

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
          toolbar: {
            children: (
              <FormControlLabel
                sx={{ margin: 'auto', paddingLeft: '20px' }}
                control={
                  <IOSSwitch
                    checked={showResolved}
                    onChange={(e) => setShowResolved(e.target.checked)}
                    sx={{ marginRight: '10px' }}
                  />
                }
                label={t("show_resolved")}
              />
            ),
          },
        }}
      />

      {/* Details / resolve dialog */}
      {openedRow !== undefined && (
        <ReportDetails
          row={openedRow}
          onClose={() => setOpenedRow(undefined)}
        />
      )}

    </div>
  );
}
