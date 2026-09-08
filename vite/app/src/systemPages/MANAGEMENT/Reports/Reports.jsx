// -----------------------------------------------------------
//  [*] MANAGEMENT — Reports
//
//  The moderation queue page: page chrome, the translated
//  title, and the reports DataGrid sized to fill the rest of
//  the viewport. All the logic lives in ReportsTable.
//
//  Visible to both panel roles — resolving reports is the
//  curator's daily job.
// -----------------------------------------------------------

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import ReportsTable from "./ReportsTable/ReportsTable";
import { useTranslations } from '@/i18n';







// -----------------------------------------------------------
// ReportsPage (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /reports (via PageWrapper)
// -----------------------------------------------------------

export default function ReportsPage({ authData }) {

  const t = useTranslations("PAGES.reports");

  return (
    <PageLayout authData={authData}>
      <div className="h-full p-5 flex flex-col">

        <PageTitle>{t("TITLE")}</PageTitle>

        {/* The reports table fills the rest of the viewport */}
        <div className="flex-1 min-h-0">
          <ReportsTable />
        </div>

      </div>
    </PageLayout>
  );
}
