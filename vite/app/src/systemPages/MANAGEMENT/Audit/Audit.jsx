// -----------------------------------------------------------
//  [*] MANAGEMENT — Audit (the console's own trail)
//
//  Every privileged mutation the backend performs writes one
//  audit row — minting/revoking codes, role and active
//  changes, erasures, broadcasts, report decisions,
//  tombstone restores. This page is the read side
//  (GET /api/admin/audit, admin only): who did what to which
//  target, newest first, with the action's context rendered
//  as compact JSON. Read-only by nature — the trail has no
//  edit path anywhere.
//
//  The newest 500 rows are fetched (the trail grows forever;
//  older forensics belong in the DB console).
//
//  Split into (root component last):
//
//    Audit_Columns — column set built from t
//    Audit         — the page itself (default export)
// -----------------------------------------------------------

import { useMemo } from "react";
import { DataGrid, GridLogicOperator } from "@mui/x-data-grid";
import { LinearProgress } from '@mui/material';
import { useQuery } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { dateTimeColumn } from '@/utils/timestamps';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import QuickSearchToolbar from '@/components/DatagridCustomComponents/QuickSearchToolbar';
import CustomPagination from '@/components/other/ButtonsPagination/ButtonsPagination';


// How much of the trail one visit loads
const AUDIT_LIMIT = 500;







// -----------------------------------------------------------
// Audit_Columns
// -----------------------------------------------------------
//
// The column set. A function (not a const) because the
// headers need t(); memoized by the caller. The action and
// payload render monospace — they are identifiers and JSON,
// not prose.
//
// Used by:
//   - Audit (below)
// -----------------------------------------------------------

function Audit_Columns(t) {
  return [
    {
      field: "createdAt",
      headerName: t("COLUMNS.createdAt"),
      width: 160,
      ...dateTimeColumn,
    },
    {
      field: "actorName",
      headerName: t("COLUMNS.actor"),
      width: 160,
      valueFormatter: (value) => value ?? t("erased_actor"),
    },
    {
      field: "action",
      headerName: t("COLUMNS.action"),
      width: 190,
      renderCell: (params) => <span className="font-mono text-xs">{params.value}</span>,
    },
    {
      field: "target",
      headerName: t("COLUMNS.target"),
      width: 240,
      renderCell: (params) => <span className="font-mono text-xs">{params.value}</span>,
    },
    {
      field: "payload",
      headerName: t("COLUMNS.payload"),
      flex: 1,
      minWidth: 240,
      valueGetter: (value) => (value == null ? '' : JSON.stringify(value)),
      renderCell: (params) => <span className="font-mono text-xs">{params.value}</span>,
    },
  ];
}







// -----------------------------------------------------------
// Audit (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /audit (via PageWrapper);
//     admin only (the sidebar hides it from curators)
// -----------------------------------------------------------

export default function Audit({ authData }) {

  const t = useTranslations("PAGES.audit");

  const { active } = useKeepAliveContext();
  const { data = {}, isLoading: loadingData } = useQuery({
    queryKey: ['admin', 'audit'],
    queryFn: async () => (await api.get('/api/admin/audit', { params: { limit: AUDIT_LIMIT } })).data,
    enabled: active,
  });

  const columns = useMemo(() => Audit_Columns(t), [t]);


  return (
    <PageLayout authData={authData}>
      <div className="h-full p-5 flex flex-col">

        <PageTitle>{t("TITLE")}</PageTitle>

        <div className="flex-1 min-h-0 rounded-[15px] bg-white p-4 shadow-card">
          <DataGrid
            sx={{ height: '100%', border: 'none' }}
            rows={data.audit ?? []}
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
