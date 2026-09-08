// -----------------------------------------------------------
//  [*] Providers — i18n + MUI theme
//
//  Wraps the app in the IntlProvider (src/i18n.jsx) and the
//  MUI ThemeProvider (single light theme from src/theme.js).
//  The theme lives INSIDE the i18n provider so it can follow
//  the active locale: when the locale is "lt", the DataGrid
//  texts (overlays, column menu, filter panel, ...) are
//  merged in as theme defaults — one place instead of a
//  localeText prop on every grid.
//
//  Dark mode is deliberately not supported.
// -----------------------------------------------------------

import { useMemo } from 'react';
import { ThemeProvider, createTheme } from '@mui/material/styles';
import CssBaseline from '@mui/material/CssBaseline';
import { IntlProvider, useLocale } from '@/i18n';
import theme, { themeOptions } from '@/theme';
import dataGridLocaleLt from '@/dataGridLocaleLt';







// -----------------------------------------------------------
// LocaleTheme
// -----------------------------------------------------------
//
// ThemeProvider that follows the app locale: Lithuanian gets
// the theme rebuilt with the DataGrid localeText defaults
// merged in, English uses the base theme as-is (the grid's
// built-in texts are already English). Grids must NOT pass
// their own localeText prop — it would replace this default
// instead of merging with it.
//
// Used by:
//   - Providers (below)
// -----------------------------------------------------------

function LocaleTheme({ children }) {

  const locale = useLocale();

  const localizedTheme = useMemo(() => {
    if (locale !== 'lt') return theme;
    return createTheme(themeOptions, {
      components: {
        MuiDataGrid: { defaultProps: { localeText: dataGridLocaleLt } },
      },
    });
  }, [locale]);

  return (
    <ThemeProvider theme={localizedTheme}>
      <CssBaseline />
      {children}
    </ThemeProvider>
  );
}







// -----------------------------------------------------------
// Providers (default export)
// -----------------------------------------------------------
//
// Used by:
//   - App.jsx — wraps every page except /login
// -----------------------------------------------------------

export default function Providers({ children }) {
  return (
    <IntlProvider>
      <LocaleTheme>
        {children}
      </LocaleTheme>
    </IntlProvider>
  );
}
