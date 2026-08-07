-- Dedicated Order Book bullets from PPT / concall (transcript).
-- Often shown on capital-goods, infra, engineering decks.
-- Run once in Supabase SQL editor (after add_tone_watch_next.sql if not yet run).

ALTER TABLE public.transcript_summaries
  ADD COLUMN IF NOT EXISTS order_book jsonb;

ALTER TABLE public.ppt_summaries
  ADD COLUMN IF NOT EXISTS order_book jsonb;
