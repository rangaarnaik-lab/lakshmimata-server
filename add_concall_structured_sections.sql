-- Earnings-call highlights: dynamic sections (Management Commentary,
-- Financial Performance, Segment Performance, etc.) + report title.
-- Run once in Supabase SQL editor.

ALTER TABLE public.transcript_summaries
  ADD COLUMN IF NOT EXISTS structured_sections jsonb,
  ADD COLUMN IF NOT EXISTS report_title text,
  ADD COLUMN IF NOT EXISTS key_takeaway text;

CREATE INDEX IF NOT EXISTS transcript_summaries_structured_gin
  ON public.transcript_summaries USING gin (structured_sections);
