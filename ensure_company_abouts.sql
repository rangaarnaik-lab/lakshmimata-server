-- AI-generated company briefs (what they do, customers, segments, innovation).
-- Populated by fundamentals_worker from existing PPT/concall summary text via Gemini.
-- Run once in Supabase SQL editor.

  CREATE TABLE IF NOT EXISTS public.company_abouts (
  symbol text PRIMARY KEY,
  overall_brief text,
  what_they_do text,
  customers jsonb,
  segments jsonb,
  innovation jsonb,
  sources jsonb,
  website text,
  image_url text,
  source_ppt_url text,
  source_tx_url text,
  source_announced_at timestamptz,
  status text,
  updated_at timestamptz DEFAULT now()
);

ALTER TABLE public.company_abouts ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'company_abouts' AND policyname = 'company_abouts_public_read'
  ) THEN
    CREATE POLICY company_abouts_public_read
      ON public.company_abouts FOR SELECT
      TO anon, authenticated
      USING (true);
  END IF;
END $$;

GRANT SELECT ON public.company_abouts TO anon, authenticated;
GRANT ALL ON public.company_abouts TO service_role;

-- Worker upserts via SUPABASE_KEY. If that key is the anon/publishable key
-- (not service_role), INSERT/UPDATE needs an explicit policy or saves
-- appear to succeed while rows never stick and About loops the same symbol.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'company_abouts' AND policyname = 'company_abouts_write'
  ) THEN
    CREATE POLICY company_abouts_write
      ON public.company_abouts FOR ALL
      TO anon, authenticated, service_role
      USING (true)
      WITH CHECK (true);
  END IF;
END $$;

GRANT INSERT, UPDATE, DELETE ON public.company_abouts TO anon, authenticated;
