// -----------------------------------------------------------
//  [*] MANAGEMENT — Users
//
//  The user directory page (admin only — the sidebar hides it
//  from curators and the backend would answer their calls
//  with 403): page chrome, the translated title, and the
//  users DataGrid sized to fill the rest of the viewport.
//  All the logic lives in UsersTable.
// -----------------------------------------------------------

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import UsersTable from "./UsersTable/UsersTable";
import { useTranslations } from '@/i18n';







// -----------------------------------------------------------
// UsersPage (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /users (via PageWrapper)
// -----------------------------------------------------------

export default function UsersPage({ authData }) {

  const t = useTranslations("PAGES.users");

  return (
    <PageLayout authData={authData}>
      <div className="h-full p-5 flex flex-col">

        <PageTitle>{t("TITLE")}</PageTitle>

        {/* The users table fills the rest of the viewport */}
        <div className="flex-1 min-h-0">
          <UsersTable authData={authData} />
        </div>

      </div>
    </PageLayout>
  );
}
