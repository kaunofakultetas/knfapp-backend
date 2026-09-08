// -----------------------------------------------------------
//  [*] CONTENT — Memes (the sticker library)
//
//  The meme library the mobile chat picker draws from
//  (GET /api/memes — 60 per page with ?offset, ?q matches
//  every word against title+tags with diacritics folded).
//  Rendered as an image card wall, not a grid of rows —
//  these are pictures, the filename tells nothing.
//
//  Moderation: DELETE /api/memes/<id> is pusher-or-admin on
//  the backend, so the hold-to-delete overlay renders for
//  admins only (a curator would collect 403s).
//
//  Split into (root component last):
//
//    MemeCard — one image card + admin delete overlay
//    Memes    — search + wall + load more (default export)
// -----------------------------------------------------------

import { useEffect, useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { useKeepAliveContext } from 'keepalive-for-react';
import { Button, CircularProgress, TextField } from '@mui/material';

import api from '@/api/client';
import { useTranslations } from '@/i18n';

import PageLayout from "@/systemPages/PageLayout";
import PageTitle from "@/components/PageTitle/PageTitle";
import { LongPressDeleteButton } from '@/components/LongPressButton';







// -----------------------------------------------------------
// MemeCard
// -----------------------------------------------------------
//
// One meme: the image with its title underneath and — for
// admins — the hold-to-delete button. Deleting removes the
// row AND the stored file.
//
// Used by:
//   - Memes (below) — one per library entry
// -----------------------------------------------------------

function MemeCard({ meme, canDelete, onDelete, t }) {
  return (
    <div className="w-[180px] rounded-[12px] bg-white shadow-card overflow-hidden flex flex-col">

      <img
        src={meme.url}
        alt={meme.title}
        loading="lazy"
        className="w-full h-[140px] object-contain bg-gray-100"
      />

      <div className="p-2 flex flex-col gap-1">
        <span className="text-xs font-semibold truncate">{meme.title}</span>
        {meme.tags && <span className="text-[10px] text-muted truncate">{meme.tags}</span>}

        {canDelete && (
          <LongPressDeleteButton
            duration={1500}
            size="small"
            onComplete={() => onDelete(meme.id)}
            completedToastMessage={t("DELETE.completed_toast")}
            uncompletedToastMessage={t("DELETE.uncompleted_toast")}
          >
            {t("DELETE.button")}
          </LongPressDeleteButton>
        )}
      </div>

    </div>
  );
}







// -----------------------------------------------------------
// Memes (default export)
// -----------------------------------------------------------
//
// Used by:
//   - router.jsx — route /memes (via PageWrapper)
// -----------------------------------------------------------

export default function Memes({ authData }) {

  const t = useTranslations("PAGES.memes");
  const canDelete = authData?.role === 'admin';
  const queryClient = useQueryClient();

  // The input updates immediately; the query key only after a
  // short pause, so every keystroke is not a request
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => setQ(input.trim()), 300);
    return () => clearTimeout(timer);
  }, [input]);


  // The library, 60 per page — the next offset is simply how
  // many cards are already on screen
  const { active } = useKeepAliveContext();
  const { data, isLoading, isFetchingNextPage, hasNextPage, fetchNextPage } = useInfiniteQuery({
    queryKey: ['memes', q],
    queryFn: async ({ pageParam }) => (await api.get('/api/memes', {
      params: { ...(q ? { q } : {}), ...(pageParam ? { offset: pageParam } : {}) },
    })).data,
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) =>
      lastPage.hasMore ? allPages.reduce((n, page) => n + page.memes.length, 0) : undefined,
    enabled: active,
  });

  const memes = data?.pages?.flatMap((page) => page.memes) ?? [];


  const remove = useMutation({
    mutationFn: async (memeId) => (await api.delete(`/api/memes/${memeId}`)).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['memes'] });
    },
  });


  return (
    <PageLayout authData={authData} backgroundColor="#EBECEF">
      <div className="p-5">

        <PageTitle>
          {t("TITLE")}
          <TextField
            size="small"
            placeholder={t("search")}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            sx={{ width: 260, backgroundColor: 'white' }}
          />
        </PageTitle>

        {/* The wall */}
        {isLoading ? (
          <div className="flex justify-center p-10"><CircularProgress /></div>
        ) : (
          <div className="flex flex-wrap gap-3">
            {memes.map((meme) => (
              <MemeCard
                key={meme.id}
                meme={meme}
                canDelete={canDelete}
                onDelete={(memeId) => remove.mutate(memeId)}
                t={t}
              />
            ))}
            {memes.length === 0 && (
              <div className="text-sm text-muted p-4">{t("empty")}</div>
            )}
          </div>
        )}

        {/* Next 60 */}
        {hasNextPage && (
          <div className="flex justify-center mt-4">
            <Button variant="outlined" disabled={isFetchingNextPage} onClick={() => fetchNextPage()}>
              {isFetchingNextPage ? t("loading") : t("load_more")}
            </Button>
          </div>
        )}

      </div>
    </PageLayout>
  );
}
