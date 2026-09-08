// -----------------------------------------------------------
//  [*] OPERATIONS — Wayfind
//
//  The indoor-map buildings page: page chrome, the
//  translated title, and the buildings DataGrid sized to
//  fill the rest of the viewport. All the logic lives in
//  BuildingsTable.
//
//  The map CONTENT (floors, nodes, rooms) is edited in the
//  mobile app's admin tools — this page owns the lifecycle
//  around it: creating a building, watching draft vs
//  published revisions, publishing, and the version history.
// -----------------------------------------------------------

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import BuildingsTable from "./BuildingsTable/BuildingsTable";
import { useTranslations } from '@/i18n';







// -----------------------------------------------------------
// WayfindPage (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /wayfind (via PageWrapper)
// -----------------------------------------------------------

export default function WayfindPage({ authData }) {

  const t = useTranslations("PAGES.wayfind");

  return (
    <PageLayout authData={authData}>
      <div className="h-full p-5 flex flex-col">

        <PageTitle>{t("TITLE")}</PageTitle>

        {/* The buildings table fills the rest of the viewport */}
        <div className="flex-1 min-h-0">
          <BuildingsTable authData={authData} />
        </div>

      </div>
    </PageLayout>
  );
}
