-- Daily NSE cash-market FII/FPI & DII flows (₹ Crore).
-- Populated by live_scan.fetch_and_store_fii_dii_daily (NSE + ~6mo history-full).
-- One-shot history: run backfill_fii_dii_6months.sql after this table exists.
-- Frontend: Market Overview mini card + Smart Money → Daily FII/DII panel.

CREATE TABLE IF NOT EXISTS public.fii_dii_daily (
  trade_date date PRIMARY KEY,
  fii_buy    numeric,
  fii_sell   numeric,
  fii_net    numeric,
  dii_buy    numeric,
  dii_sell   numeric,
  dii_net    numeric,
  fetched_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS fii_dii_daily_fetched_at_idx
  ON public.fii_dii_daily (fetched_at DESC);

ALTER TABLE public.fii_dii_daily ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'fii_dii_daily'
      AND policyname = 'fii_dii_daily_public_read'
  ) THEN
    CREATE POLICY fii_dii_daily_public_read
      ON public.fii_dii_daily FOR SELECT
      TO anon, authenticated
      USING (true);
  END IF;
END $$;

GRANT SELECT ON public.fii_dii_daily TO anon, authenticated;
GRANT ALL ON public.fii_dii_daily TO service_role;
