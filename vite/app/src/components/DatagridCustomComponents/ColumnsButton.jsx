// -----------------------------------------------------------
//  [*] DataGrid custom components — ColumnsButton
//
//  Toolbar button for MUI X DataGrid that toggles the built-in
//  column visibility panel (show/hide columns). A replacement
//  for the stock GridToolbarColumnsButton, styled as a small
//  contained button with a custom label (falls back to the
//  translated stock label when the caller gives none).
//
//  Must be rendered inside a DataGrid toolbar slot — it relies
//  on useGridApiContext to reach the grid API.
// -----------------------------------------------------------

import { useState } from 'react';
import { GridPreferencePanelsValue, useGridApiContext } from '@mui/x-data-grid';
import { Button } from '@mui/material';
import ViewColumnIcon from '@mui/icons-material/ViewColumn';

import { useTranslations } from '@/i18n';







// -----------------------------------------------------------
// ColumnsButton (default export)
// -----------------------------------------------------------
//
// Tracks the panel state locally so a second click closes the
// panel instead of reopening it.
//
// Used by:
//   - QuickSearchToolbar — the toolbar of every DataGrid in
//     the app
// -----------------------------------------------------------

export default function ColumnsButton({ label }) {

  const t = useTranslations("COMPONENTS.datagrid");
  const apiRef = useGridApiContext();
  const [panelOpen, setPanelOpen] = useState(false);

  return (
    <Button
      variant="contained"
      color="primary"
      size="small"
      startIcon={<ViewColumnIcon />}
      onClick={() => {
        if (panelOpen) {
          apiRef.current.hidePreferences();
          setPanelOpen(false);
        } else {
          apiRef.current.showPreferences(GridPreferencePanelsValue.columns);
          setPanelOpen(true);
        }
      }}
      sx={{ ml: 1 }}
    >
      {label ?? t("columns")}
    </Button>
  );
}
