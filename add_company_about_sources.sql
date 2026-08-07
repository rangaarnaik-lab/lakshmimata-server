-- Optional sources list from Gemini Google Search grounding.
-- Safe to run even if company_abouts already exists.
ALTER TABLE public.company_abouts
  ADD COLUMN IF NOT EXISTS sources jsonb;
