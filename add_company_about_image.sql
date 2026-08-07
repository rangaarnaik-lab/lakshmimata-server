-- Company logo / website for About Company tab.
-- Run in Supabase SQL Editor if About saves fail with:
--   Could not find the 'image_url' column of 'company_abouts' in the schema cache
-- Then: Project Settings → API → Reload schema (if available), or wait ~1 min.

ALTER TABLE public.company_abouts
  ADD COLUMN IF NOT EXISTS website text;

ALTER TABLE public.company_abouts
  ADD COLUMN IF NOT EXISTS image_url text;

NOTIFY pgrst, 'reload schema';
