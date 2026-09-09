// -----------------------------------------------------------
//  [*] CONTENT — Uploads (stored files)
//
//  The stored-file ledger (GET /api/admin/uploads, admin
//  only): every file accounted to an owner, plus the
//  ownerless rows an account erasure leaves behind — the
//  files an admin most wants to find and sweep. Clicking a
//  row opens the details dialog with the public link and
//  the hold-to-confirm delete (the existing owner-or-admin
//  DELETE /api/uploads/<filename> — the file AND its row go
//  together).
//
//  Split into (root component last):
//
//    formatBytes     — byte count → readable size
//    FileDetails     — the open-link + delete dialog
//    Uploads_Columns — column set built from t
//    Uploads         — the page itself (default export)
// -----------------------------------------------------------

import { useMemo, useState } from "react";
import { DataGrid, GridLogicOperator } from "@mui/x-data-grid";
import { LinearProgress } from '@mui/material';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { dateTimeColumn, formatDateTime } from '@/utils/timestamps';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import QuickSearchToolbar from '@/components/DatagridCustomComponents/QuickSearchToolbar';
import CustomPagination from '@/components/other/ButtonsPagination/ButtonsPagination';
import UniversalModal from '@/components/UniversalModal';
import { LongPressDeleteButton } from '@/components/LongPressButton';


// Byte count → "1.2 MB" (the ledger stores exact bytes; the
// grid reads better rounded)
const formatBytes = (bytes) => {
  if (bytes == null) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};







// -----------------------------------------------------------
// FileDetails
// -----------------------------------------------------------
//
// One stored file up close: owner, size, the public link,
// and the hold-to-confirm delete.
//
// Used by:
//   - Uploads (below) — opened on row click
// -----------------------------------------------------------

function FileDetails({ row, onClose, t }) {

  const queryClient = useQueryClient();

  const remove = useMutation({
    mutationFn: async () => (await api.delete(`/api/uploads/${row.filename}`)).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['admin', 'uploads'] });
      onClose();
    },
  });

  return (
    <UniversalModal
      open={true}   // the parent mounts/unmounts instead of toggling
      onClose={onClose}
      title={row.filename}
      description={`${row.userName ?? t("ownerless")} · ${formatBytes(row.size)} · ${formatDateTime(row.createdAt)}`}
      maxWidth={440}
      fullWidth
      actions={
        <LongPressDeleteButton
          duration={1500}
          fullWidth
          disabled={remove.isPending}
          onComplete={() => remove.mutate()}
          completedToastMessage={t("DELETE.completed_toast")}
          uncompletedToastMessage={t("DELETE.uncompleted_toast")}
        >
          {t("DELETE.button")}
        </LongPressDeleteButton>
      }
    >
      <a href={row.url} target="_blank" rel="noopener noreferrer"
         className="text-primary text-sm underline flex items-center gap-1">
        {t("open_file")} <OpenInNewIcon sx={{ fontSize: 16 }} />
      </a>
    </UniversalModal>
  );
}







// -----------------------------------------------------------
// Uploads_Columns
// -----------------------------------------------------------
//
// The column set. A function (not a const) because the
// headers need t(); memoized by the caller.
//
// Used by:
//   - Uploads (below)
// -----------------------------------------------------------

function Uploads_Columns(t) {
  return [
    {
      field: "filename",
      headerName: t("COLUMNS.filename"),
      flex: 1,
      minWidth: 240,
      renderCell: (params) => <span className="font-mono text-xs">{params.value}</span>,
    },
    {
      field: "userName",
      headerName: t("COLUMNS.owner"),
      width: 170,
      renderCell: (params) => (
        params.value
          ? params.value
          : <span className="italic text-muted">{t("ownerless")}</span>
      ),
    },
    {
      field: "size",
      headerName: t("COLUMNS.size"),
      width: 110,
      type: "number",
      valueFormatter: (value) => formatBytes(value),
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
// Uploads (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /uploads (via PageWrapper);
//     admin only (the sidebar hides it from curators)
// -----------------------------------------------------------

export default function Uploads({ authData }) {

  const t = useTranslations("PAGES.uploads");

  // The dialog is mounted/unmounted rather than toggled
  const [openedRow, setOpenedRow] = useState(undefined);

  const { active } = useKeepAliveContext();
  const { data = {}, isLoading: loadingData } = useQuery({
    queryKey: ['admin', 'uploads'],
    queryFn: async () => (await api.get('/api/admin/uploads')).data,
    enabled: active,
  });

  const columns = useMemo(() => Uploads_Columns(t), [t]);


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
            rows={data.uploads ?? []}
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

        {/* Details / delete dialog */}
        {openedRow !== undefined && (
          <FileDetails row={openedRow} onClose={() => setOpenedRow(undefined)} t={t} />
        )}

      </div>
    </PageLayout>
  );
}
