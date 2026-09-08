// -----------------------------------------------------------
//  [*] CONTENT — News
//
//  The news/feed content page: page chrome, the translated
//  title, and the posts DataGrid sized to fill the rest of
//  the viewport. All the logic lives in NewsTable.
//
//  Staff accounts see everything the feed holds — including
//  private faculty drafts the public never gets.
// -----------------------------------------------------------

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import NewsTable from "./NewsTable/NewsTable";
import { useTranslations } from '@/i18n';







// -----------------------------------------------------------
// NewsPage (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /news (via PageWrapper)
// -----------------------------------------------------------

export default function NewsPage({ authData }) {

  const t = useTranslations("PAGES.news");

  return (
    <PageLayout authData={authData}>
      <div className="h-full p-5 flex flex-col">

        <PageTitle>{t("TITLE")}</PageTitle>

        {/* The posts table fills the rest of the viewport */}
        <div className="flex-1 min-h-0">
          <NewsTable authData={authData} />
        </div>

      </div>
    </PageLayout>
  );
}
