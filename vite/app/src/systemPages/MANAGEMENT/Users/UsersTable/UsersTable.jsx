// -----------------------------------------------------------
//  [*] MANAGEMENT — UsersTable
//
//  The user directory DataGrid, one row per account from
//  GET /api/admin/users (TanStack query ['admin','users'],
//  fetched once — edits refetch it through invalidation).
//  Clicking a row opens the EditUser dialog for that account.
//
//  Role and account state render as solid pills; the
//  timestamps sort on the real date.
//
//  Split into (root component last):
//
//    ROLE_COLORS        — role → pill color
//    UsersTable_Columns — column set built from t
//    UsersTable         — the grid itself (default export)
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
import EditUser from '../EditUser/EditUser';


// Solid pill colors per role — the two staff roles wear the
// brand shades, the everyday roles stay muted
const ROLE_COLORS = {
  admin: '#7B003F',
  curator: '#E64164',
  teacher: 'steelblue',
  student: 'grey',
};







// -----------------------------------------------------------
// UsersTable_Columns
// -----------------------------------------------------------
//
// The column set. A function (not a const) because the
// headers and pill texts need t(); memoized by the caller.
//
// Used by:
//   - UsersTable (below)
// -----------------------------------------------------------

function UsersTable_Columns(t) {
  return [
    {
      field: "username",
      headerName: t("COLUMNS.username"),
      flex: 1,
      minWidth: 140,
    },
    {
      field: "displayName",
      headerName: t("COLUMNS.displayName"),
      flex: 1,
      minWidth: 160,
    },
    {
      field: "email",
      headerName: t("COLUMNS.email"),
      flex: 1,
      minWidth: 200,
    },
    {
      field: "role",
      headerName: t("COLUMNS.role"),
      width: 110,
      renderCell: (params) => (
        <div
          className="px-1.5 rounded-md w-[90px] text-center text-white"
          style={{ backgroundColor: ROLE_COLORS[params.value] ?? 'grey' }}
        >
          {t(`ROLES.${params.value}`)}
        </div>
      ),
    },
    {
      field: "active",
      headerName: t("COLUMNS.active"),
      width: 110,
      renderCell: (params) => (
        <div
          className="px-1.5 rounded-md w-[90px] text-center text-white"
          style={{ backgroundColor: params.value ? 'green' : 'red' }}
        >
          {params.value ? t("STATE.active") : t("STATE.inactive")}
        </div>
      ),
    },
    {
      field: "createdAt",
      headerName: t("COLUMNS.createdAt"),
      width: 160,
      ...dateTimeColumn,
    },
  ];
}







// -----------------------------------------------------------
// UsersTable (default export)
// -----------------------------------------------------------
//
// Used by:
//   - Users.jsx
// -----------------------------------------------------------

export default function UsersTable({ authData }) {

  const t = useTranslations("PAGES.users");

  // The dialog is mounted/unmounted rather than toggled —
  // undefined means closed, a grid row means editing it
  const [editedRow, setEditedRow] = useState(undefined);

  const { active } = useKeepAliveContext();
  const { data = {}, isLoading: loadingData } = useQuery({
    queryKey: ['admin', 'users'],
    queryFn: async () => (await api.get('/api/admin/users')).data,
    enabled: active,
  });

  const columns = useMemo(() => UsersTable_Columns(t), [t]);


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
        rows={data.users ?? []}
        columns={columns}
        rowHeight={30}
        pageSizeOptions={[100]}
        showToolbar
        loading={loadingData}
        onRowClick={(params) => setEditedRow(params.row)}

        initialState={{
          filter: {
            filterModel: {
              items: [],
              quickFilterLogicOperator: GridLogicOperator.Or,
              quickFilterExcludeHiddenColumns: false,
            },
          },
          sorting: {
            sortModel: [{ field: 'createdAt', sort: 'desc' }],
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

      {/* Edit dialog — mounted only while a row is open */}
      {editedRow !== undefined && (
        <EditUser
          row={editedRow}
          authData={authData}
          onClose={() => setEditedRow(undefined)}
        />
      )}

    </div>
  );
}
