-- Allow the app (anon key) to read stock_fundamentals for About / Fundamentals /
-- Emerging Themes / Flags. Diagnosed 2026-08-07: anon GETs returned [] (RLS)
-- while PPT/transcript tables were readable — so Themes/Highlights/Flags looked
-- permanently empty even when the worker wrote rows.
-- Also adds industry when missing (worker prompts; sector stays on stocks).

ALTER TABLE public.stock_fundamentals
  ADD COLUMN IF NOT EXISTS industry text;

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
