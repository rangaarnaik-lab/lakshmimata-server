-- Ask AI + management Flags (Screener-style diligence).
-- 1) Precomputed flags on stock_fundamentals (always visible)
-- 2) On-demand Q&A queue answered by the fundamentals worker

ALTER TABLE public.stock_fundamentals
  ADD COLUMN IF NOT EXISTS mgmt_verdict text,
  ADD COLUMN IF NOT EXISTS mgmt_summary text,
  ADD COLUMN IF NOT EXISTS mgmt_flags jsonb,
  ADD COLUMN IF NOT EXISTS mgmt_flags_at timestamptz;

CREATE TABLE IF NOT EXISTS public.stock_ai_asks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol text NOT NULL,
  question text NOT NULL,
  status text NOT NULL DEFAULT 'pending',  -- pending | done | error
  answer text,
  verdict text,
  flags jsonb,
  sources jsonb,
  error text,
  visitor_id text,
  user_id uuid,
  created_at timestamptz NOT NULL DEFAULT now(),
  answered_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_stock_ai_asks_pending
  ON public.stock_ai_asks (created_at)
  WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_stock_ai_asks_symbol_created
  ON public.stock_ai_asks (symbol, created_at DESC);

ALTER TABLE public.stock_ai_asks ENABLE ROW LEVEL SECURITY;

-- Anyone can submit a question and read answers (Gemini key stays on the worker).
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'stock_ai_asks' AND policyname = 'stock_ai_asks_public_insert'
  ) THEN
    CREATE POLICY stock_ai_asks_public_insert
      ON public.stock_ai_asks FOR INSERT
      TO anon, authenticated
      WITH CHECK (
        status = 'pending'
        AND char_length(trim(question)) BETWEEN 8 AND 400
        AND char_length(trim(symbol)) BETWEEN 1 AND 20
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'stock_ai_asks' AND policyname = 'stock_ai_asks_public_select'
  ) THEN
    CREATE POLICY stock_ai_asks_public_select
      ON public.stock_ai_asks FOR SELECT
      TO anon, authenticated
      USING (true);
  END IF;
END $$;

GRANT SELECT, INSERT ON public.stock_ai_asks TO anon, authenticated;
GRANT ALL ON public.stock_ai_asks TO service_role;
