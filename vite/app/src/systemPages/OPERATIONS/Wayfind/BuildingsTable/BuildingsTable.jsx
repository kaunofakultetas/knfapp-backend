// -----------------------------------------------------------
//  [*] OPERATIONS — BuildingsTable
//
//  The wayfind buildings DataGrid from
//  GET /api/wayfind/buildings (TanStack query
//  ['wayfind','buildings'], refetched through invalidation
//  after creating/publishing). The toolbar's "add new"
//  (admin only) opens the NewBuilding dialog; clicking a row
//  opens the building's lifecycle dialog.
//
//  "Pending changes" is derived: the draft revision counter
//  running ahead of the published revision means edits are
//  waiting to be published.
//
//  Split into (root component last):
//
//    BuildingsTable_Columns — column set built from t
//    BuildingsTable         — the grid itself (default export)
// -----------------------------------------------------------

import { useMemo, useState } from "react";
import { DataGrid, GridLogicOperator } from "@mui/x-data-grid";
import { LinearProgress } from '@mui/material';
import { useQuery } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { dateTimeColumn } from '@/utils/timestamps';

import QuickSearchToolbar from '@/components/DatagridCustomComponents/QuickSearchToolbar';
import CustomPagination from '@/components/other/ButtonsPagination/ButtonsPagination';
import NewBuilding from '../NewBuilding/NewBuilding';
import BuildingDetails from '../BuildingDetails/BuildingDetails';







// -----------------------------------------------------------
// BuildingsTable_Columns
// -----------------------------------------------------------
//
// The column set. A function (not a const) because the
// headers and pill texts need t(); memoized by the caller.
//
// Used by:
//   - BuildingsTable (below)
// -----------------------------------------------------------

function BuildingsTable_Columns(t) {
  return [
    {
      field: "id",
      headerName: t("COLUMNS.id"),
      width: 140,
      renderCell: (params) => <span className="font-mono">{params.value}</span>,
    },
    {
      field: "name",
      headerName: t("COLUMNS.name"),
      flex: 1,
      minWidth: 200,
    },
    {
      field: "publishedRevision",
      headerName: t("COLUMNS.published"),
      width: 130,
      renderCell: (params) => (
        <div
          className="px-1.5 rounded-md w-[110px] text-center text-white"
          style={{ backgroundColor: params.value != null ? 'green' : 'grey' }}
        >
          {params.value != null ? `v${params.value}` : t("STATE.unpublished")}
        </div>
      ),
    },
    {
      field: "draftRevision",
      headerName: t("COLUMNS.draft"),
      width: 100,
      valueFormatter: (value) => `v${value}`,
    },
    {
      field: "pending",
      headerName: t("COLUMNS.pending"),
      width: 150,
      valueGetter: (value, row) => row.draftRevision > (row.publishedRevision ?? 0),
      renderCell: (params) => (
        <div
          className="px-1.5 rounded-md w-[130px] text-center text-white"
          style={{ backgroundColor: params.value ? 'orange' : 'green' }}
        >
          {params.value ? t("STATE.pending_changes") : t("STATE.up_to_date")}
        </div>
      ),
    },
    {
      field: "publishedAt",
      headerName: t("COLUMNS.publishedAt"),
      width: 160,
      ...dateTimeColumn,
    },
  ];
}







// -----------------------------------------------------------
// BuildingsTable (default export)
// -----------------------------------------------------------
//
// Used by:
//   - Wayfind.jsx
// -----------------------------------------------------------

export default function BuildingsTable({ authData }) {

  const t = useTranslations("PAGES.wayfind");
  const isAdmin = authData?.role === 'admin';

  // Both dialogs are mounted/unmounted rather than toggled
  const [creating, setCreating] = useState(false);
  const [openedRow, setOpenedRow] = useState(undefined);

  const { active } = useKeepAliveContext();
  const { data = {}, isLoading: loadingData } = useQuery({
    queryKey: ['wayfind', 'buildings'],
    queryFn: async () => (await api.get('/api/wayfind/buildings')).data,
    enabled: active,
  });

  const columns = useMemo(() => BuildingsTable_Columns(t), [t]);


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
        rows={data.buildings ?? []}
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
          toolbar: isAdmin ? {
            addNewLabel: t("new_building"),
            onAddNew: () => setCreating(true),
          } : {},
        }}
      />

      {/* Create dialog — creating buildings is admin-only */}
      {creating && (
        <NewBuilding onClose={() => setCreating(false)} />
      )}

      {/* Lifecycle dialog */}
      {openedRow !== undefined && (
        <BuildingDetails
          row={openedRow}
          authData={authData}
          onClose={() => setOpenedRow(undefined)}
        />
      )}

    </div>
  );
}
