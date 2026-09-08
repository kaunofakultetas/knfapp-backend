// -----------------------------------------------------------
//  [*] MANAGEMENT — Invitations
//
//  The invitation codes page: page chrome, the translated
//  title, and the invitations DataGrid sized to fill the
//  rest of the viewport. All the logic lives in
//  InvitationsTable.
//
//  Visible to both panel roles — the backend scopes the
//  data itself (admins see every code, curators only the
//  student/teacher codes they minted).
// -----------------------------------------------------------

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import InvitationsTable from "./InvitationsTable/InvitationsTable";
import { useTranslations } from '@/i18n';







// -----------------------------------------------------------
// InvitationsPage (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /invitations (via PageWrapper)
// -----------------------------------------------------------

export default function InvitationsPage({ authData }) {

  const t = useTranslations("PAGES.invitations");

  return (
    <PageLayout authData={authData}>
      <div className="h-full p-5 flex flex-col">

        <PageTitle>{t("TITLE")}</PageTitle>

        {/* The invitations table fills the rest of the viewport */}
        <div className="flex-1 min-h-0">
          <InvitationsTable authData={authData} />
        </div>

      </div>
    </PageLayout>
  );
}
