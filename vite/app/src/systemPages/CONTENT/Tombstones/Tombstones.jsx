// -----------------------------------------------------------
//  [*] CONTENT — Tombstones (the scraper skip-list)
//
//  Deleting a scraped article tombstones its source URL so
//  the scrapers can never resurrect it. This page is the
//  list of those tombstones (GET /api/admin/tombstones,
//  admin only) and the way BACK: restoring one
//  (POST /api/admin/tombstones/restore) lets the article
//  return on the scraper's next pass over a page that still
//  carries it — nothing is re-fetched eagerly, which is why
//  the dialog says so.
//
//  Split into (root component last):
//
//    RestoreDialog      — confirm + what restoring means
//    Tombstones_Columns — column set built from t
//    Tombstones         — the page itself (default export)
// -----------------------------------------------------------

import { useMemo, useState } from "react";
import { DataGrid, GridLogicOperator } from "@mui/x-data-grid";
import { LinearProgress } from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';
import toast from 'react-hot-toast';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { dateTimeColumn } from '@/utils/timestamps';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import QuickSearchToolbar from '@/components/DatagridCustomComponents/QuickSearchToolbar';
import CustomPagination from '@/components/other/ButtonsPagination/ButtonsPagination';
import UniversalModal from '@/components/UniversalModal';







// -----------------------------------------------------------
// RestoreDialog
// -----------------------------------------------------------
//
// Confirms the lift: the URL, and the note that the article
// only comes back with the next scrape of a page that still
// links it.
//
// Used by:
//   - Tombstones (below) — opened on row click
// -----------------------------------------------------------

function RestoreDialog({ row, onClose, t }) {

  const queryClient = useQueryClient();

  const restore = useMutation({
    mutationFn: async () => (await api.post('/api/admin/tombstones/restore', {
      source_url: row.sourceUrl,
    })).data,
    onSuccess: () => {
      toast.success(<b>{t("restored_toast")}</b>, { duration: 3000 });
      queryClient.invalidateQueries({ queryKey: ['admin', 'tombstones'] });
      onClose();
    },
  });

  return (
    <UniversalModal
      open={true}   // the parent mounts/unmounts instead of toggling
      onClose={onClose}
      title={t("restore_title")}
      variant="warning"
      maxWidth={460}
      fullWidth
      confirmText={t("restore")}
      closeOnConfirm={false}   // the mutation closes on success itself
      loading={restore.isPending}
      onConfirm={() => restore.mutate()}
    >
      <div className="flex flex-col gap-3">
        <span className="font-mono text-xs bg-gray-50 border border-edge rounded-lg px-3 py-2 break-all">
          {row.sourceUrl}
        </span>
        <span className="text-xs text-muted">{t("restore_note")}</span>
      </div>
    </UniversalModal>
  );
}







// -----------------------------------------------------------
// Tombstones_Columns
// -----------------------------------------------------------
//
// The column set. A function (not a const) because the
// headers need t(); memoized by the caller.
//
// Used by:
//   - Tombstones (below)
// -----------------------------------------------------------

function Tombstones_Columns(t) {
  return [
    {
      field: "sourceUrl",
      headerName: t("COLUMNS.sourceUrl"),
      flex: 1,
      minWidth: 320,
      renderCell: (params) => <span className="font-mono text-xs">{params.value}</span>,
    },
    {
      field: "deletedByName",
      headerName: t("COLUMNS.deletedBy"),
      width: 170,
      valueFormatter: (value) => value ?? '',
    },
    {
      field: "deletedAt",
      headerName: t("COLUMNS.deletedAt"),
      width: 160,
      ...dateTimeColumn,
    },
  ];
}







// -----------------------------------------------------------
// Tombstones (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /tombstones (via PageWrapper);
//     admin only (the sidebar hides it from curators)
// -----------------------------------------------------------

export default function Tombstones({ authData }) {

  const t = useTranslations("PAGES.tombstones");

  // The dialog is mounted/unmounted rather than toggled
  const [openedRow, setOpenedRow] = useState(undefined);

  const { active } = useKeepAliveContext();
  const { data = {}, isLoading: loadingData } = useQuery({
    queryKey: ['admin', 'tombstones'],
    queryFn: async () => (await api.get('/api/admin/tombstones')).data,
    enabled: active,
  });

  const columns = useMemo(() => Tombstones_Columns(t), [t]);

  // The grid wants an `id` field; the table's key is the URL
  const rows = (data.tombstones ?? []).map((row) => ({ id: row.sourceUrl, ...row }));


  return (
    <PageLayout authData={authData}>
      <div className="h-full p-5 flex flex-col">

        <PageTitle>{t("TITLE")}</PageTitle>

        <div className="flex-1 min-h-0 rounded-[15px] bg-white p-4 shadow-card">
          <DataGrid
            sx={{
              height: '100%',
              cursor: 'pointer',
              border: 'none',
              '& .MuiDataGrid-row:hover': {
                backgroundColor: 'rgba(123, 0, 63, 0.08)',
              },
            }}
            rows={rows}
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
            }}
          />
        </div>

        {/* Restore dialog */}
        {openedRow !== undefined && (
          <RestoreDialog row={openedRow} onClose={() => setOpenedRow(undefined)} t={t} />
        )}

      </div>
    </PageLayout>
  );
}
