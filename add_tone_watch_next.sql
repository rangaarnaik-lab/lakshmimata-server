-- Tone + Watch Next for Concall (transcript) and PPT summaries.
-- Additive only — existing sections stay as they are.
-- Run once in Supabase SQL editor.

ALTER TABLE public.transcript_summaries
  ADD COLUMN IF NOT EXISTS management_tone text,
  ADD COLUMN IF NOT EXISTS watch_next jsonb;

ALTER TABLE public.ppt_summaries
  ADD COLUMN IF NOT EXISTS management_tone text,
  ADD COLUMN IF NOT EXISTS watch_next jsonb;
