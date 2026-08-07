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
