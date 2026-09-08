// -----------------------------------------------------------
//  [*] DataGrid custom components — QuickSearchToolbar
//
//  The shared DataGrid toolbar shell: an always-expanded
//  quick filter (input is trimmed before matching), the
//  ColumnsButton and — only when onAddNew is given — the
//  burgundy "add new" ToolbarButton. Page-specific extras
//  (filter switches, counters, pickers) come in as children
//  and render after the buttons, inside the same Toolbar row.
//
//  Props:
//    - placeholder  — quick filter placeholder text; when not
//                     given, the translated default is used
//                     ("Ieškoti..." / "Search...")
//    - columnsLabel — ColumnsButton label; when not given the
//                     button keeps its own default
//    - addNewLabel  — label of the "add new" button
//    - onAddNew     — click handler of the "add new" button;
//                     the button renders only when this is set
//    - children     — page-specific toolbar extras
// -----------------------------------------------------------

import { Toolbar, QuickFilter, QuickFilterControl } from "@mui/x-data-grid";
import AddCircleOutlinedIcon from '@mui/icons-material/AddCircleOutlined';

import { useTranslations } from "@/i18n";
import ColumnsButton from '@/components/DatagridCustomComponents/ColumnsButton';
import ToolbarButton from '@/components/DatagridCustomComponents/ToolbarButton';







// -----------------------------------------------------------
// QuickSearchToolbar (default export)
// -----------------------------------------------------------
//
// label={columnsLabel} is passed even when undefined — the
// ColumnsButton then falls back to its own translated label,
// so callers without a custom label still get the stock one.
//
// Used by:
//   - every DataGrid in the app — users, invitations,
//     reports, news, schedule, scraper runs, wayfind
//     buildings
// -----------------------------------------------------------

export default function QuickSearchToolbar({ placeholder, columnsLabel, addNewLabel, onAddNew, children }) {

  const t = useTranslations("COMPONENTS.datagrid");

  return (
    <Toolbar sx={{ justifyContent: 'flex-start', flexWrap: 'wrap', rowGap: '4px' }}>
      <QuickFilter
        expanded
        parser={(searchInput) => [searchInput.trim()]}
        formatter={(quickFilterValues) => quickFilterValues.join('')}
      >
        <QuickFilterControl placeholder={placeholder ?? t("searchPlaceholder")} size="small" />
      </QuickFilter>
      <ColumnsButton label={columnsLabel} />
      {onAddNew && <ToolbarButton label={addNewLabel} icon={AddCircleOutlinedIcon} onClick={onAddNew} />}
      {children}
    </Toolbar>
  );
}
