// -----------------------------------------------------------
//  [*] OPERATIONS — NewBuilding modal
//
//  Registers a wayfind building (POST /api/wayfind/buildings,
//  admin only). The id becomes part of every map URL and can
//  never be changed — the field enforces the backend's slug
//  shape (lowercase letters/digits/dashes) as the user types.
//  The fresh building starts empty at draft revision 0; the
//  actual mapping happens in the mobile app's editor.
//
//  Used by:
//    - BuildingsTable — the toolbar's "new building" button
// -----------------------------------------------------------

import { useState } from "react";
import { useMutation, useQueryClient } from '@tanstack/react-query';
import toast from 'react-hot-toast';
import { TextField } from "@mui/material";

import api from '@/api/client';
import { useTranslations } from '@/i18n';
import UniversalModal from '@/components/UniversalModal';


// The backend's id shape: ^[a-z0-9][a-z0-9-]{0,63}$
const ID_SHAPE = /^[a-z0-9][a-z0-9-]{0,63}$/;


export default function NewBuilding({ onClose }) {

  const t = useTranslations("PAGES.wayfind");
  const queryClient = useQueryClient();

  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [northDeg, setNorthDeg] = useState("");


  const create = useMutation({
    mutationFn: async () => (await api.post('/api/wayfind/buildings', {
      id,
      name: name.trim(),
      ...(northDeg !== "" ? { northDeg: Number(northDeg) } : {}),
    })).data,
    onSuccess: () => {
      toast.success(<b>{t("created_toast")}</b>, { duration: 3000 });
      queryClient.invalidateQueries({ queryKey: ['wayfind', 'buildings'] });
      onClose();
    },
  });


  // Type-through normalisation: whatever is typed becomes a
  // valid slug prefix (uppercase folded, other characters
  // dropped) so the submit never fails on shape
  const handleIdChange = (value) => {
    setId(value.toLowerCase().replace(/[^a-z0-9-]/g, '').replace(/^-+/, '').slice(0, 64));
  };

  const submittable = ID_SHAPE.test(id) && name.trim() !== "";


  return (
    <UniversalModal
      open={true}   // always open — the parent mounts/unmounts this component instead
      onClose={onClose}
      title={t("new_building")}
      maxWidth={420}
      fullWidth
      confirmText={t("create")}
      confirmDisabled={!submittable}
      closeOnConfirm={false}   // the mutation closes on success itself
      loading={create.isPending}
      onConfirm={() => create.mutate()}
    >
      <div className="flex flex-col gap-4 pt-1">

        <TextField
          label={t("FIELDS.id")}
          value={id}
          onChange={(e) => handleIdChange(e.target.value)}
          helperText={t("FIELDS.id_hint")}
          fullWidth
        />

        <TextField
          label={t("FIELDS.name")}
          value={name}
          onChange={(e) => setName(e.target.value)}
          fullWidth
        />

        <TextField
          type="number"
          label={t("FIELDS.north")}
          value={northDeg}
          onChange={(e) => setNorthDeg(e.target.value)}
          helperText={t("FIELDS.north_hint")}
          fullWidth
        />

      </div>
    </UniversalModal>
  );
}
