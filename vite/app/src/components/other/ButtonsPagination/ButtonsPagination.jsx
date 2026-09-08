// -----------------------------------------------------------
//  [*] Other — ButtonsPagination
//
//  Custom pagination footer for MUI X DataGrid: replaces the
//  default "rows per page" footer with numbered page buttons
//  (outlined, rounded, first/last arrows, up to 4 boundary
//  pages). Selected/hover colors are the app's burgundy/pink.
//
//  Must be rendered inside a DataGrid slot — it reads and sets
//  the page via useGridApiContext. Note the off-by-one: the
//  grid counts pages from 0, the Pagination widget from 1.
// -----------------------------------------------------------

import { gridPageCountSelector, gridPageSelector, useGridApiContext, useGridSelector } from "@mui/x-data-grid";
import Pagination from '@mui/material/Pagination';
import PaginationItem from '@mui/material/PaginationItem';







// -----------------------------------------------------------
// CustomPagination (default export)
// -----------------------------------------------------------
//
// Used by:
//   - every DataGrid in the app — passed as the pagination
//     slot (users, invitations, reports, news, schedule,
//     scraper runs, wayfind buildings)
// -----------------------------------------------------------

export default function CustomPagination() {

  const apiRef = useGridApiContext();
  const page = useGridSelector(apiRef, gridPageSelector);
  const pageCount = useGridSelector(apiRef, gridPageCountSelector);

  return (
    <Pagination
      boundaryCount={4}
      showFirstButton
      showLastButton
      variant="outlined"
      shape="rounded"
      page={page + 1}
      count={pageCount}
      renderItem={(props2) => <PaginationItem {...props2} disableRipple />}
      onChange={(event, value) => apiRef.current.setPage(value - 1)}
      sx={{
        '& .MuiPaginationItem-root': {
          '&.Mui-selected': {
            background: 'var(--mui-palette-primary-main)',
            color: 'white',
            "&:hover": {
              backgroundColor: 'primary.dark',
            },
          },
          "&:hover": {
            backgroundColor: 'primary.dark',
          },
        },
      }}
    />
  );
}
