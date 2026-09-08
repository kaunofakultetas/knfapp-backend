// -----------------------------------------------------------
//  [*] MANAGEMENT — Broadcast (push announcement)
//
//  Sends a push notification to every device that has the
//  "admin" notification channel enabled (the app's opt-OUT
//  model — this reaches essentially everyone, which is why
//  the form carries a standing warning).
//
//  POST /api/admin/notifications answers 202 with a job id
//  immediately; the delivery fans out on the backend. The
//  page then polls GET /api/admin/notifications/<jobId>
//  every 2 seconds until the job reports done/failed and
//  shows the ticket counts. The job registry is in-memory on
//  the backend (last 50 jobs, gone on restart) — a 404 on
//  the poll is reported as "no longer tracked", not an error.
//
//  Split into (root component last):
//
//    JOB_COLORS — job status → pill color
//    JobCard    — the submitted job's live status card
//    Broadcast  — the form + submit (default export)
// -----------------------------------------------------------

import { useState } from "react";
import { useMutation, useQuery } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { Button, TextField, CircularProgress } from "@mui/material";
import CampaignOutlinedIcon from '@mui/icons-material/CampaignOutlined';

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import { formatDateTime } from '@/utils/timestamps';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";


// What the backend enforces on the two fields
const TITLE_MAX = 200;
const BODY_MAX = 1000;


// Job status → the house pill colors
const JOB_COLORS = {
  queued: 'grey',
  running: 'orange',
  done: 'green',
  failed: 'red',
};







// -----------------------------------------------------------
// JobCard
// -----------------------------------------------------------
//
// The submitted broadcast's live status: the pill, and once
// finished the ticket counts (sent = device tokens accepted
// by the push service, not confirmed deliveries).
//
// Used by:
//   - Broadcast (below) — after a job was submitted
// -----------------------------------------------------------

function JobCard({ job, lost, t }) {

  if (lost) {
    return (
      <div className="rounded-[12px] border border-edge bg-gray-50 p-3 text-sm text-muted">
        {t("job_lost")}
      </div>
    );
  }

  if (!job) return null;

  const finished = job.status === 'done' || job.status === 'failed';

  return (
    <div className="rounded-[12px] border border-edge p-3">

      <div className="flex items-center gap-3">
        <div
          className="px-2 rounded-md text-white text-xs text-center min-w-[90px]"
          style={{ backgroundColor: JOB_COLORS[job.status] ?? 'grey' }}
        >
          {t(`JOB.${job.status}`)}
        </div>
        <span className="text-sm font-semibold">{job.title}</span>
        {!finished && <CircularProgress size={14} />}
      </div>

      {finished && (
        <div className="text-xs text-muted mt-2">
          {t("job_result", {
            sent: job.sent,
            failed: job.failed,
            users: job.distinctUsers,
          })}
          {job.finishedAt ? ` · ${formatDateTime(job.finishedAt)}` : ''}
        </div>
      )}

    </div>
  );
}







// -----------------------------------------------------------
// Broadcast (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /broadcast (via PageWrapper);
//     admin only (the sidebar hides it from curators)
// -----------------------------------------------------------

export default function Broadcast({ authData }) {

  const t = useTranslations("PAGES.broadcast");

  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [jobId, setJobId] = useState(null);


  // Poll the submitted job until it settles; a 404 means the
  // in-memory registry dropped it (restart / 50 newer jobs)
  const { data: job, isError: jobLost } = useQuery({
    queryKey: ['admin', 'broadcast', jobId],
    queryFn: async () => (await api.get(`/api/admin/notifications/${jobId}`)).data,
    enabled: Boolean(jobId),
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === 'done' || status === 'failed' ? false : 2000;
    },
  });


  const send = useMutation({
    mutationFn: async () => (await api.post('/api/admin/notifications', {
      title: title.trim(),
      body: body.trim(),
    })).data,
    onSuccess: (accepted) => {
      toast.success(<b>{t("queued_toast")}</b>, { duration: 3000 });
      setJobId(accepted.jobId);
      setTitle("");
      setBody("");
    },
  });


  const submittable = title.trim() !== "" && body.trim() !== "" && !send.isPending;


  return (
    <PageLayout authData={authData}>
      <div className="p-5 max-w-[720px]">

        <PageTitle>{t("TITLE")}</PageTitle>

        <div className="rounded-[15px] bg-white p-5 shadow-card flex flex-col gap-4">

          {/* Who this reaches — the opt-out model makes this
              effectively everyone with the app installed */}
          <div className="flex gap-2 items-start text-sm text-orange-800 bg-orange-50 border border-orange-200 rounded-lg px-3 py-2">
            <CampaignOutlinedIcon fontSize="small" />
            <span>{t("audience_warning")}</span>
          </div>

          <TextField
            label={t("FIELDS.title")}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            slotProps={{ htmlInput: { maxLength: TITLE_MAX } }}
            helperText={`${title.length}/${TITLE_MAX}`}
            fullWidth
          />

          <TextField
            label={t("FIELDS.body")}
            value={body}
            onChange={(e) => setBody(e.target.value)}
            multiline
            minRows={4}
            slotProps={{ htmlInput: { maxLength: BODY_MAX } }}
            helperText={`${body.length}/${BODY_MAX}`}
            fullWidth
          />

          <Button
            variant="contained"
            disabled={!submittable}
            onClick={() => send.mutate()}
            sx={{ alignSelf: 'flex-end' }}
          >
            {t("send")}
          </Button>

        </div>

        {/* The last submitted job's fate */}
        {jobId && (
          <div className="mt-4">
            <JobCard job={job} lost={jobLost} t={t} />
          </div>
        )}

      </div>
    </PageLayout>
  );
}
