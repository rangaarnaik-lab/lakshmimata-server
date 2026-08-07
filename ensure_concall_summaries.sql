-- Run in Supabase SQL editor if Results AI summaries stay empty.
-- Diagnosed 2026-08-07: ppt_summaries / transcript_summaries have rows,
-- but concall_summaries (Results PDF AI summary) had 0 rows.
-- Prefer: resolution=merge-duplicates requires a UNIQUE/PK on
-- (symbol, attachment_url).

CREATE TABLE IF NOT EXISTS public.concall_summaries (
  symbol text NOT NULL,
  announced_at timestamptz,
  attachment_url text NOT NULL,
  summary text,
  status text,
  created_at timestamptz DEFAULT now(),
  PRIMARY KEY (symbol, attachment_url)
);

-- If the table already existed with a different PK, add the unique key
-- the worker upserts on (safe if PK already matches):
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'concall_summaries_pkey'
  ) THEN
    ALTER TABLE public.concall_summaries
      ADD CONSTRAINT concall_summaries_pkey PRIMARY KEY (symbol, attachment_url);
  END IF;
EXCEPTION WHEN others THEN
  -- If an old primary key exists on different columns, create a unique index instead
  BEGIN
    CREATE UNIQUE INDEX IF NOT EXISTS concall_summaries_symbol_attachment_uidx
      ON public.concall_summaries (symbol, attachment_url);
  EXCEPTION WHEN others THEN
    RAISE NOTICE 'Could not add unique key on (symbol, attachment_url): %', SQLERRM;
  END;
END $$;

ALTER TABLE public.concall_summaries ENABLE ROW LEVEL SECURITY;

-- Public read (anon key) so the app can show summaries
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'concall_summaries' AND policyname = 'concall_summaries_public_read'
  ) THEN
    CREATE POLICY concall_summaries_public_read
      ON public.concall_summaries FOR SELECT
      TO anon, authenticated
      USING (true);
  END IF;
END $$;

-- Service role bypasses RLS by default; inserts come from Railway worker.
GRANT SELECT ON public.concall_summaries TO anon, authenticated;
GRANT ALL ON public.concall_summaries TO service_role;
