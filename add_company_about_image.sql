-- Company website + logo image for About Company panel.
ALTER TABLE public.company_abouts
  ADD COLUMN IF NOT EXISTS website text,
  ADD COLUMN IF NOT EXISTS image_url text;
