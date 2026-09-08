// -----------------------------------------------------------
//  [*] MANAGEMENT — InvitationsTable
//
//  The invitation codes DataGrid from
//  GET /api/admin/invitations (TanStack query
//  ['admin','invitations'], refetched through invalidation
//  after minting/revoking). The toolbar's "add new" opens
//  the NewInvitation dialog; clicking a row opens the
//  details dialog with the revoke action.
//
//  Split into (root component last):
//
//    invitationState          — row → aktyvus/panaudotas/pasibaigęs
//    InvitationsTable_Columns — column set built from t
//    InvitationsTable         — the grid itself (default export)
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
import NewInvitation from '../NewInvitation/NewInvitation';
import InvitationDetails from '../InvitationDetails/InvitationDetails';


// State pill colors: green = still usable, grey = every use
// burned, red = past its expiry
const STATE_COLORS = {
  active: 'green',
  used: 'grey',
  expired: 'red',
};


// A code that is both expired and fully used counts as used —
// the burn is the more informative fact
function invitationState(row) {
  if (row.fullyUsed) return 'used';
  if (row.expired) return 'expired';
  return 'active';
}







// -----------------------------------------------------------
// InvitationsTable_Columns
// -----------------------------------------------------------
//
// The column set. A function (not a const) because the
// headers and pill texts need t(); memoized by the caller.
//
// Used by:
//   - InvitationsTable (below)
// -----------------------------------------------------------

function InvitationsTable_Columns(t) {
  return [
    {
      field: "code",
      headerName: t("COLUMNS.code"),
      width: 150,
      renderCell: (params) => (
        <span className="font-mono tracking-wider">{params.value}</span>
      ),
    },
    {
      field: "role",
      headerName: t("COLUMNS.role"),
      width: 110,
      valueFormatter: (value) => t(`ROLES.${value}`),
    },
    {
      field: "useCount",
      headerName: t("COLUMNS.uses"),
      width: 100,
      valueGetter: (value, row) => `${value} / ${row.maxUses}`,
    },
    {
      field: "state",
      headerName: t("COLUMNS.state"),
      width: 120,
      valueGetter: (value, row) => invitationState(row),
      renderCell: (params) => (
        <div
          className="px-1.5 rounded-md w-[100px] text-center text-white"
          style={{ backgroundColor: STATE_COLORS[params.value] }}
        >
          {t(`STATE.${params.value}`)}
        </div>
      ),
    },
    {
      field: "expiresAt",
      headerName: t("COLUMNS.expiresAt"),
      width: 160,
      ...dateTimeColumn,
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
// InvitationsTable (default export)
// -----------------------------------------------------------
//
// Used by:
//   - Invitations.jsx
// -----------------------------------------------------------

export default function InvitationsTable({ authData }) {

  const t = useTranslations("PAGES.invitations");

  // Both dialogs are mounted/unmounted rather than toggled
  const [minting, setMinting] = useState(false);
  const [openedRow, setOpenedRow] = useState(undefined);

  const { active } = useKeepAliveContext();
  const { data = {}, isLoading: loadingData } = useQuery({
    queryKey: ['admin', 'invitations'],
    queryFn: async () => (await api.get('/api/admin/invitations')).data,
    enabled: active,
  });

  const columns = useMemo(() => InvitationsTable_Columns(t), [t]);


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
        rows={data.invitations ?? []}
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
            addNewLabel: t("new_invitation"),
            onAddNew: () => setMinting(true),
          },
        }}
      />

      {/* Mint dialog */}
      {minting && (
        <NewInvitation
          authData={authData}
          onClose={() => setMinting(false)}
        />
      )}

      {/* Details / revoke dialog */}
      {openedRow !== undefined && (
        <InvitationDetails
          row={openedRow}
          onClose={() => setOpenedRow(undefined)}
        />
      )}

    </div>
  );
}
