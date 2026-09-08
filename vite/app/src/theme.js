// -----------------------------------------------------------
//  [*] MUI theme — CSS-variables based light theme
//
//  Single light color scheme (dark mode is deliberately not
//  supported). MUI emits every palette value as a CSS custom
//  property (--mui-*), so non-MUI styling (Tailwind arbitrary
//  values, injected CSS) can reference the same colors — e.g.
//  var(--mui-palette-primary-main) for the VU burgundy.
//
//  Palette notes:
//    - primary.main — VU burgundy #7B003F (THE brand color of
//      the mobile app and this panel; always reference it
//      through the theme, never hardcode)
//    - primary.dark — hover shade
//    - delete       — custom red palette for destructive
//                     buttons (color="delete")
//
//  Used by:
//    - providers.jsx — the default export for English, the
//      named themeOptions to rebuild the theme with the
//      Lithuanian DataGrid texts merged in
// -----------------------------------------------------------

import { createTheme } from '@mui/material/styles';
import { grey } from '@mui/material/colors';


export const themeOptions = {
  cssVariables: true,

  palette: {
    primary: {
      main: '#7B003F',
      dark: '#E64164', // Hover color
    },
    secondary: {
      main: grey[50],
    },

    delete: {
      main: '#f00000',
      dark: '#AD0000', // Hover color
      contrastText: '#fff',
    },
  },
};

const theme = createTheme(themeOptions);

export default theme;
