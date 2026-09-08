// -----------------------------------------------------------
//  [*] CONTENT — NewsTable
//
//  The posts DataGrid over GET /api/news. Unlike the other
//  grids this one paginates on the SERVER — the feed can
//  hold thousands of posts and the API caps a page at 50 —
//  so the grid runs in paginationMode="server" with the
//  page number in the query key. The quick filter therefore
//  only searches the page on screen.
//
//  The toolbar's "add new" opens the NewPost dialog (posted
//  as the signed-in staff account → a faculty post);
//  clicking a row opens the details dialog with the delete
//  action.
//
//  Split into (root component last):
//
//    SOURCE_COLORS     — post source → pill color
//    NewsTable_Columns — column set built from t
//    NewsTable         — the grid itself (default export)
// -----------------------------------------------------------

import { useMemo, useState } from "react";
import { DataGrid, GridLogicOperator } from "@mui/x-data-grid";
import { LinearProgress } from '@mui/material';
import { useQuery, keepPreviousData } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { dateTimeColumn } from '@/utils/timestamps';

import QuickSearchToolbar from '@/components/DatagridCustomComponents/QuickSearchToolbar';
import CustomPagination from '@/components/other/ButtonsPagination/ButtonsPagination';
import NewPost from '../NewPost/NewPost';
import PostDetails from '../PostDetails/PostDetails';


// The API's page size cap — also the grid's page size, so
// grid pages map 1:1 onto API pages
const PER_PAGE = 50;


// Pill colors by where a post came from: faculty burgundy,
// scraped sources muted, user wall posts grey
const SOURCE_COLORS = {
  faculty: '#7B003F',
  'knf.vu.lt': 'steelblue',
  'vu.lt': 'steelblue',
  user: 'grey',
};







// -----------------------------------------------------------
// NewsTable_Columns
// -----------------------------------------------------------
//
// The column set. A function (not a const) because the
// headers and pill texts need t(); memoized by the caller.
//
// Used by:
//   - NewsTable (below)
// -----------------------------------------------------------

function NewsTable_Columns(t) {
  return [
    {
      field: "title",
      headerName: t("COLUMNS.title"),
      flex: 1,
      minWidth: 240,
      // Wall posts carry no title — fall back to the text
      valueGetter: (value, row) => value || row.content,
    },
    {
      field: "source",
      headerName: t("COLUMNS.source"),
      width: 120,
      renderCell: (params) => (
        <div
          className="px-1.5 rounded-md w-[100px] text-center text-white"
          style={{ backgroundColor: SOURCE_COLORS[params.value] ?? 'grey' }}
        >
          {params.value}
        </div>
      ),
    },
    {
      field: "postType",
      headerName: t("COLUMNS.postType"),
      width: 130,
      valueFormatter: (value) => t(`TYPES.${value}`),
    },
    {
      field: "author",
      headerName: t("COLUMNS.author"),
      width: 160,
    },
    {
      field: "likes",
      headerName: t("COLUMNS.likes"),
      width: 90,
      type: "number",
    },
    {
      field: "comments",
      headerName: t("COLUMNS.comments"),
      width: 110,
      type: "number",
    },
    {
      field: "isPublic",
      headerName: t("COLUMNS.visibility"),
      width: 120,
      renderCell: (params) => (
        <div
          className="px-1.5 rounded-md w-[100px] text-center text-white"
          style={{ backgroundColor: params.value ? 'green' : 'grey' }}
        >
          {params.value ? t("VISIBILITY.public") : t("VISIBILITY.draft")}
        </div>
      ),
    },
    {
      field: "date",
      headerName: t("COLUMNS.date"),
      width: 160,
      ...dateTimeColumn,
    },
  ];
}







// -----------------------------------------------------------
// NewsTable (default export)
// -----------------------------------------------------------
//
// Used by:
//   - News.jsx
// -----------------------------------------------------------

export default function NewsTable({ authData }) {

  const t = useTranslations("PAGES.news");

  // Server-side page (the grid counts from 0, the API from 1)
  const [page, setPage] = useState(0);

  // Both dialogs are mounted/unmounted rather than toggled
  const [creating, setCreating] = useState(false);
  const [openedRow, setOpenedRow] = useState(undefined);

  const { active } = useKeepAliveContext();
  const { data = {}, isLoading: loadingData } = useQuery({
    queryKey: ['news', 'list', page],
    queryFn: async () => (await api.get('/api/news', {
      params: { page: page + 1, per_page: PER_PAGE },
    })).data,
    enabled: active,
    // Keep the previous page on screen while the next loads —
    // otherwise every page turn blanks the grid
    placeholderData: keepPreviousData,
  });

  const columns = useMemo(() => NewsTable_Columns(t), [t]);


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
        rows={data.posts ?? []}
        columns={columns}
        rowHeight={30}
        showToolbar
        loading={loadingData}
        onRowClick={(params) => setOpenedRow(params.row)}

        paginationMode="server"
        rowCount={data.total ?? 0}
        pageSizeOptions={[PER_PAGE]}
        paginationModel={{ page, pageSize: PER_PAGE }}
        onPaginationModelChange={(model) => setPage(model.page)}

        initialState={{
          filter: {
            filterModel: {
              items: [],
              quickFilterLogicOperator: GridLogicOperator.Or,
              quickFilterExcludeHiddenColumns: false,
            },
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
            addNewLabel: t("new_post"),
            onAddNew: () => setCreating(true),
          },
        }}
      />

      {/* Create dialog */}
      {creating && (
        <NewPost onClose={() => setCreating(false)} />
      )}

      {/* Details / delete dialog */}
      {openedRow !== undefined && (
        <PostDetails
          row={openedRow}
          authData={authData}
          onClose={() => setOpenedRow(undefined)}
        />
      )}

    </div>
  );
}
