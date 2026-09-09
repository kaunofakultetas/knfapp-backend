// -----------------------------------------------------------
//  [*] CONTENT — NewPost modal
//
//  Publishes a post as the signed-in staff account
//  (POST /api/news — a staff author lands in the feed as a
//  faculty post). Title, content, a type select
//  (announcement / article / link / social) and the
//  public/draft switch; a draft stays visible to staff only
//  until it is deleted or re-created public (there is no
//  edit endpoint — the wire contract has none).
//
//  Used by:
//    - NewsTable — the toolbar's "new post" button
// -----------------------------------------------------------

import { useState } from "react";
import { useMutation, useQueryClient } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { MenuItem, TextField, FormControlLabel } from "@mui/material";

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import UniversalModal from '@/components/UniversalModal';
import IOSSwitch from '@/components/other/IOSSwitch/IOSSwitch';


// The types a hand-written post may carry ("poll" exists in
// the feed but is created through its own poll flow)
const POST_TYPES = ['announcement', 'article', 'link', 'social'];


export default function NewPost({ onClose }) {

  const t = useTranslations("PAGES.news");
  const queryClient = useQueryClient();

  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [postType, setPostType] = useState('announcement');
  const [isPublic, setIsPublic] = useState(true);


  const create = useMutation({
    mutationFn: async () => (await api.post('/api/news', {
      title: title.trim(),
      content: content.trim(),
      post_type: postType,
      is_public: isPublic,
    })).data,
    onSuccess: () => {
      toast.success(<b>{t("created_toast")}</b>, { duration: 3000 });
      queryClient.invalidateQueries({ queryKey: ['news', 'list'] });
      onClose();
    },
  });


  return (
    <UniversalModal
      open={true}   // the parent mounts/unmounts instead of toggling
      onClose={onClose}
      title={t("new_post")}
      maxWidth={560}
      fullWidth
      confirmText={t("publish")}
      confirmDisabled={content.trim() === ""}
      closeOnConfirm={false}   // the mutation closes on success itself
      loading={create.isPending}
      onConfirm={() => create.mutate()}
    >
      <div className="flex flex-col gap-4 pt-1">

        <TextField
          label={t("FIELDS.title")}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          fullWidth
        />

        <TextField
          label={t("FIELDS.content")}
          value={content}
          onChange={(e) => setContent(e.target.value)}
          multiline
          minRows={5}
          fullWidth
        />

        <TextField
          select
          label={t("COLUMNS.postType")}
          value={postType}
          onChange={(e) => setPostType(e.target.value)}
          fullWidth
        >
          {POST_TYPES.map((type) => (
            <MenuItem key={type} value={type}>{t(`TYPES.${type}`)}</MenuItem>
          ))}
        </TextField>

        <FormControlLabel
          sx={{ ml: 0 }}
          control={
            <IOSSwitch
              checked={isPublic}
              onChange={(e) => setIsPublic(e.target.checked)}
              sx={{ marginRight: '10px' }}
            />
          }
          label={isPublic ? t("VISIBILITY.public") : t("VISIBILITY.draft")}
        />

      </div>
    </UniversalModal>
  );
}
