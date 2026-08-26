-- Queue for on-demand fundamentals refill (missing ratios on the tab, or a
-- genuine dislike with a comment). The worker reads pending rows, scrapes
-- Upstox, and optionally regenerates AI takeaways. Never persist numbers
-- from Gemini — only from load_fundamentals_batch / XBRL.

CREATE TABLE IF NOT EXISTS public.fundamentals_heal_requests (
  symbol        text PRIMARY KEY,
  reason        text NOT NULL DEFAULT 'missing'
                  CHECK (reason IN ('missing', 'dislike')),
  section_key   text,
  comment       text,
  force         boolean NOT NULL DEFAULT false,
  status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'working', 'done', 'failed', 'rejected')),
  requested_at  timestamptz NOT NULL DEFAULT now(),
  processed_at  timestamptz,
  last_error    text
);

CREATE INDEX IF NOT EXISTS idx_fundamentals_heal_pending
  ON public.fundamentals_heal_requests (requested_at ASC)
  WHERE status = 'pending';

ALTER TABLE public.fundamentals_heal_requests ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS fundamentals_heal_public_upsert ON public.fundamentals_heal_requests;
CREATE POLICY fundamentals_heal_public_upsert
  ON public.fundamentals_heal_requests FOR INSERT
  TO anon, authenticated
  WITH CHECK (true);

DROP POLICY IF EXISTS fundamentals_heal_public_update ON public.fundamentals_heal_requests;
CREATE POLICY fundamentals_heal_public_update
  ON public.fundamentals_heal_requests FOR UPDATE
  TO anon, authenticated
  USING (true)
  WITH CHECK (true);

DROP POLICY IF EXISTS fundamentals_heal_public_select ON public.fundamentals_heal_requests;
CREATE POLICY fundamentals_heal_public_select
  ON public.fundamentals_heal_requests FOR SELECT
  TO anon, authenticated
  USING (true);

GRANT SELECT, INSERT, UPDATE ON public.fundamentals_heal_requests TO anon, authenticated;
GRANT ALL ON public.fundamentals_heal_requests TO service_role;

COMMENT ON TABLE public.fundamentals_heal_requests IS
  'User/tab-triggered refill of stock_fundamentals from Upstox. Dislike with a comment sets force=true.';
