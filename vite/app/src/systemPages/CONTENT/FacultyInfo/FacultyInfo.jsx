// -----------------------------------------------------------
//  [*] CONTENT — FacultyInfo (handbook viewer)
//
//  Read-only window into the faculty handbook the mobile app
//  serves (GET /api/info?lang=..): the curated content with
//  the scraped overlay already merged in, exactly as a phone
//  would receive it. Sections render as accordions with the
//  raw JSON inside — this page is for VERIFYING what the app
//  ships, not for editing (the handbook has no write path;
//  fresh data comes from the info scraper).
//
//  Split into (root component last):
//
//    SectionAccordion — one top-level section, JSON inside
//    FacultyInfo      — lang toggle + sections (default export)
// -----------------------------------------------------------

import { useState } from "react";
import { Accordion, AccordionSummary, AccordionDetails, ToggleButton, ToggleButtonGroup } from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import { useQuery } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { formatDateTime } from '@/utils/timestamps';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import PageLoading from "@/components/PageLoading/PageLoading";


// Response fields that are metadata, not handbook sections
const META_FIELDS = ['lang', 'updatedAt'];







// -----------------------------------------------------------
// SectionAccordion
// -----------------------------------------------------------
//
// One handbook section: the key as the summary, the raw
// pretty-printed JSON as the body (scrollable sideways for
// wide values).
//
// Used by:
//   - FacultyInfo (below) — one per top-level key
// -----------------------------------------------------------

function SectionAccordion({ name, value }) {
  return (
    <Accordion disableGutters>
      <AccordionSummary expandIcon={<ExpandMoreIcon />}>
        <span className="font-semibold text-sm">{name}</span>
      </AccordionSummary>
      <AccordionDetails>
        <pre className="text-xs bg-gray-50 border border-edge rounded-lg p-3 overflow-x-auto m-0">
          {JSON.stringify(value, null, 2)}
        </pre>
      </AccordionDetails>
    </Accordion>
  );
}







// -----------------------------------------------------------
// FacultyInfo (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /info (via PageWrapper)
// -----------------------------------------------------------

export default function FacultyInfo({ authData }) {

  const t = useTranslations("PAGES.info");

  // Which language edition of the handbook to inspect — this
  // is the CONTENT language, independent of the panel locale
  const [lang, setLang] = useState('lt');

  const { active } = useKeepAliveContext();
  const { data, isLoading } = useQuery({
    queryKey: ['info', lang],
    queryFn: async () => (await api.get('/api/info', { params: { lang } })).data,
    enabled: active,
  });

  const sections = data
    ? Object.entries(data).filter(([key]) => !META_FIELDS.includes(key))
    : [];


  return (
    <PageLayout authData={authData}>
      <div className="p-5 max-w-[900px]">

        <PageTitle>
          {t("TITLE")}

          {/* Which edition the phone would get */}
          <ToggleButtonGroup
            exclusive
            size="small"
            value={lang}
            onChange={(e, value) => value && setLang(value)}
          >
            <ToggleButton value="lt">LT</ToggleButton>
            <ToggleButton value="en">EN</ToggleButton>
          </ToggleButtonGroup>
        </PageTitle>

        {/* When any section survived a scrape, the backend
            stamps the merge time */}
        {data?.updatedAt && (
          <div className="text-xs text-muted mb-3">
            {t("updated_at")}: {formatDateTime(data.updatedAt)}
          </div>
        )}

        {isLoading ? (
          <PageLoading />
        ) : (
          <div className="rounded-[15px] bg-white shadow-card overflow-hidden">
            {sections.map(([key, value]) => (
              <SectionAccordion key={key} name={key} value={value} />
            ))}
          </div>
        )}

      </div>
    </PageLayout>
  );
}
