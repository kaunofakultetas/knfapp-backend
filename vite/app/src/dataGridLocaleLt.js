// -----------------------------------------------------------
//  [*] DataGrid locale — Lithuanian texts for MUI X DataGrid
//
//  MUI X ships no Lithuanian locale (@mui/x-data-grid/locales
//  has no ltLT), so the strings live here. This is NOT part of
//  src/messages on purpose: the grid's built-in texts
//  ("No rows", column menu, filter panel, ...) go through
//  MUI's own localeText mechanism, and English needs no file
//  at all — the grid's defaults ARE the English texts. Only
//  the Lithuanian overrides exist, keyed exactly like MUI's
//  enUS locale.
//
//  Partial on purpose: keys that cannot appear in this app
//  (density switcher, pivot/charts/AI toolbar, tree data,
//  checkbox selection, ...) are left out — the grid falls
//  back to its English default for any missing key.
//
//  Used by:
//    - providers.jsx — merged into the theme as the DataGrid
//      localeText default when the locale is "lt"
// -----------------------------------------------------------

const dataGridLocaleLt = {

  // Overlays
  noRowsLabel: 'Nėra duomenų',
  noResultsOverlayLabel: 'Įrašų nerasta.',
  noColumnsOverlayLabel: 'Nėra stulpelių',

  // Stock toolbar buttons (the shared QuickSearchToolbar has
  // its own labels — these cover the built-in ones)
  toolbarColumns: 'Stulpeliai',
  toolbarColumnsLabel: 'Pasirinkti stulpelius',
  toolbarFilters: 'Filtrai',
  toolbarFiltersLabel: 'Rodyti filtrus',
  toolbarFiltersTooltipHide: 'Slėpti filtrus',
  toolbarFiltersTooltipShow: 'Rodyti filtrus',
  toolbarQuickFilterPlaceholder: 'Ieškoti...',
  toolbarQuickFilterLabel: 'Ieškoti',
  toolbarQuickFilterDeleteIconLabel: 'Išvalyti',
  toolbarExport: 'Eksportuoti',
  toolbarExportLabel: 'Eksportuoti',
  toolbarExportCSV: 'Atsisiųsti CSV',
  toolbarExportPrint: 'Spausdinti',

  // Columns management panel
  columnsManagementSearchTitle: 'Ieškoti',
  columnsManagementNoColumns: 'Stulpelių nėra',
  columnsManagementShowHideAllText: 'Rodyti/slėpti visus',
  columnsManagementReset: 'Atstatyti',
  columnsManagementDeleteIconLabel: 'Išvalyti',

  // Filter panel
  filterPanelAddFilter: 'Pridėti filtrą',
  filterPanelRemoveAll: 'Pašalinti visus',
  filterPanelDeleteIconLabel: 'Ištrinti',
  filterPanelLogicOperator: 'Loginis operatorius',
  filterPanelOperator: 'Operatorius',
  filterPanelOperatorAnd: 'Ir',
  filterPanelOperatorOr: 'Arba',
  filterPanelColumns: 'Stulpeliai',
  filterPanelInputLabel: 'Reikšmė',
  filterPanelInputPlaceholder: 'Filtro reikšmė',

  // Filter operators
  filterOperatorContains: 'turi',
  filterOperatorDoesNotContain: 'neturi',
  filterOperatorEquals: 'lygu',
  filterOperatorDoesNotEqual: 'nelygu',
  filterOperatorStartsWith: 'prasideda',
  filterOperatorEndsWith: 'baigiasi',
  filterOperatorIs: 'yra',
  filterOperatorNot: 'nėra',
  filterOperatorAfter: 'vėliau nei',
  filterOperatorOnOrAfter: 'vėliau arba tuo pačiu metu',
  filterOperatorBefore: 'anksčiau nei',
  filterOperatorOnOrBefore: 'anksčiau arba tuo pačiu metu',
  filterOperatorIsEmpty: 'tuščias',
  filterOperatorIsNotEmpty: 'netuščias',
  filterOperatorIsAnyOf: 'bet kuris iš',
  filterValueAny: 'bet kokia',
  filterValueTrue: 'taip',
  filterValueFalse: 'ne',

  // Column menu
  columnMenuLabel: 'Meniu',
  columnMenuShowColumns: 'Rodyti stulpelius',
  columnMenuManageColumns: 'Tvarkyti stulpelius',
  columnMenuFilter: 'Filtruoti',
  columnMenuHideColumn: 'Slėpti stulpelį',
  columnMenuUnsort: 'Naikinti rikiavimą',
  columnMenuSortAsc: 'Rikiuoti didėjančiai',
  columnMenuSortDesc: 'Rikiuoti mažėjančiai',
  columnHeaderSortIconLabel: 'Rikiuoti',

  // Footer
  footerRowSelected: (count) => `Pasirinkta: ${count}`,
  footerTotalRows: 'Iš viso eilučių:',
  footerTotalVisibleRows: (visibleCount, totalCount) =>
    `${visibleCount.toLocaleString()} iš ${totalCount.toLocaleString()}`,

  // Cells
  booleanCellTrueLabel: 'taip',
  booleanCellFalseLabel: 'ne',
  actionsCellMore: 'daugiau',
};

export default dataGridLocaleLt;
