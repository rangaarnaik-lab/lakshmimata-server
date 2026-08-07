-- Allow the web app (anon key) to read stock_fundamentals.
-- Diagnosed 2026-08-07: anon SELECT returned 0 rows (RLS on, no public policy),
-- so About / Fundamentals / Themes / Flags tabs stayed empty even when the
-- worker had written data.
-- Also ensures theme + AI highlight + Flags columns exist.

ALTER TABLE public.stock_fundamentals ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'stock_fundamentals'
      AND policyname = 'stock_fundamentals_public_read'
  ) THEN
    CREATE POLICY stock_fundamentals_public_read
      ON public.stock_fundamentals FOR SELECT
      TO anon, authenticated
      USING (true);
  END IF;
END $$;

GRANT SELECT ON public.stock_fundamentals TO anon, authenticated;
GRANT ALL ON public.stock_fundamentals TO service_role;

-- Theme columns (Emerging Themes under Market Cap)
ALTER TABLE public.stock_fundamentals
  ADD COLUMN IF NOT EXISTS emerging_themes jsonb,
  ADD COLUMN IF NOT EXISTS theme_evidence jsonb,
  ADD COLUMN IF NOT EXISTS theme_intensity text,
  ADD COLUMN IF NOT EXISTS themes_source text,
  ADD COLUMN IF NOT EXISTS themes_at timestamptz,
  ADD COLUMN IF NOT EXISTS themes_announced_at timestamptz;

-- Fundamentals tab AI takeaways
ALTER TABLE public.stock_fundamentals
  ADD COLUMN IF NOT EXISTS ai_highlights jsonb,
  ADD COLUMN IF NOT EXISTS ai_key_metrics jsonb,
  ADD COLUMN IF NOT EXISTS ai_highlights_at timestamptz;

-- Management Flags (Ask AI companion)
ALTER TABLE public.stock_fundamentals
  ADD COLUMN IF NOT EXISTS mgmt_verdict text,
  ADD COLUMN IF NOT EXISTS mgmt_summary text,
  ADD COLUMN IF NOT EXISTS mgmt_flags jsonb,
  ADD COLUMN IF NOT EXISTS mgmt_flags_at timestamptz;
