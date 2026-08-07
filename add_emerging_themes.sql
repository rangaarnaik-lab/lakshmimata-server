-- Emerging theme fields for PPT + transcript AI summaries.
-- Run in Supabase SQL Editor after deploying the worker that writes these columns.

ALTER TABLE public.transcript_summaries
  ADD COLUMN IF NOT EXISTS emerging_themes jsonb,
  ADD COLUMN IF NOT EXISTS theme_evidence jsonb,
  ADD COLUMN IF NOT EXISTS theme_intensity text;

ALTER TABLE public.ppt_summaries
  ADD COLUMN IF NOT EXISTS emerging_themes jsonb,
  ADD COLUMN IF NOT EXISTS theme_evidence jsonb,
  ADD COLUMN IF NOT EXISTS theme_intensity text;

-- Optional: GIN index so Themes radar can filter "contains theme" cheaply
CREATE INDEX IF NOT EXISTS transcript_summaries_themes_gin
  ON public.transcript_summaries USING gin (emerging_themes);
CREATE INDEX IF NOT EXISTS ppt_summaries_themes_gin
  ON public.ppt_summaries USING gin (emerging_themes);
