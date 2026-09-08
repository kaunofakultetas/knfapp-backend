// -----------------------------------------------------------
//  [*] Router — the app's route table
//
//  createBrowserRouter setup with basename /adminpanel (the
//  prefix the panel is served under — see vite.config.js).
//  Every page is a child of the App layout route ("/"), which
//  adds the auth and theme providers plus KeepAlive page
//  caching (see App.jsx). Pages render through PageWrapper,
//  which injects authData.
//
//  Route groups (one per systemPages folder):
//    - _OTHERS     — login (rendered bare, no providers)
//    - HOME        — the landing dashboard
//    - MANAGEMENT  — users, invitations, reports, broadcast
//    - CONTENT     — news, schedule, faculty info
//    - OPERATIONS  — scrapers, wayfind buildings
//    - SYSTEM      — account
//
//  Used by:
//    - main.jsx — passed to RouterProvider
// -----------------------------------------------------------

import { createBrowserRouter } from 'react-router-dom';
import App from '@/App';

// Login
import Login from '@/systemPages/_OTHERS/Login/Login';

// Home
import Home from '@/systemPages/HOME/Home/Home';

// Management
import Users from '@/systemPages/MANAGEMENT/Users/Users';
import Invitations from '@/systemPages/MANAGEMENT/Invitations/Invitations';
import Reports from '@/systemPages/MANAGEMENT/Reports/Reports';
import Broadcast from '@/systemPages/MANAGEMENT/Broadcast/Broadcast';
import Audit from '@/systemPages/MANAGEMENT/Audit/Audit';

// Content
import News from '@/systemPages/CONTENT/News/News';
import Memes from '@/systemPages/CONTENT/Memes/Memes';
import Uploads from '@/systemPages/CONTENT/Uploads/Uploads';
import Tombstones from '@/systemPages/CONTENT/Tombstones/Tombstones';
import Schedule from '@/systemPages/CONTENT/Schedule/Schedule';
import FacultyInfo from '@/systemPages/CONTENT/FacultyInfo/FacultyInfo';

// Operations
import Scrapers from '@/systemPages/OPERATIONS/Scrapers/Scrapers';
import Wayfind from '@/systemPages/OPERATIONS/Wayfind/Wayfind';

// System
import Account from '@/systemPages/SYSTEM/Account/Account';

import PageWrapper from '@/PageWrapper';


// Every page except /login goes through PageWrapper (authData
// as a prop); App itself skips the providers on /login
export const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      // Login — rendered bare (App skips the providers)
      { path: 'login', element: <Login /> },

      // Home
      { index: true, element: <PageWrapper component={Home} /> },

      // Management
      { path: 'users', element: <PageWrapper component={Users} /> },
      { path: 'invitations', element: <PageWrapper component={Invitations} /> },
      { path: 'reports', element: <PageWrapper component={Reports} /> },
      { path: 'broadcast', element: <PageWrapper component={Broadcast} /> },
      { path: 'audit', element: <PageWrapper component={Audit} /> },

      // Content
      { path: 'news', element: <PageWrapper component={News} /> },
      { path: 'memes', element: <PageWrapper component={Memes} /> },
      { path: 'uploads', element: <PageWrapper component={Uploads} /> },
      { path: 'tombstones', element: <PageWrapper component={Tombstones} /> },
      { path: 'schedule', element: <PageWrapper component={Schedule} /> },
      { path: 'info', element: <PageWrapper component={FacultyInfo} /> },

      // Operations
      { path: 'scrapers', element: <PageWrapper component={Scrapers} /> },
      { path: 'wayfind', element: <PageWrapper component={Wayfind} /> },

      // System
      { path: 'account', element: <PageWrapper component={Account} /> },
    ],
  },
], { basename: '/adminpanel' });
