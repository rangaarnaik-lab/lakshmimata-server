-- Same on-demand refill as 020, for quarterly results numbers (XBRL).
-- Never persist numbers from Gemini — only fetch_and_save_result_for_announcement.

CREATE TABLE IF NOT EXISTS public.results_heal_requests (
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

CREATE INDEX IF NOT EXISTS idx_results_heal_pending
  ON public.results_heal_requests (requested_at ASC)
  WHERE status = 'pending';

ALTER TABLE public.results_heal_requests ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS results_heal_public_upsert ON public.results_heal_requests;
CREATE POLICY results_heal_public_upsert
  ON public.results_heal_requests FOR INSERT
  TO anon, authenticated
  WITH CHECK (true);

DROP POLICY IF EXISTS results_heal_public_update ON public.results_heal_requests;
CREATE POLICY results_heal_public_update
  ON public.results_heal_requests FOR UPDATE
  TO anon, authenticated
  USING (true)
  WITH CHECK (true);

DROP POLICY IF EXISTS results_heal_public_select ON public.results_heal_requests;
CREATE POLICY results_heal_public_select
  ON public.results_heal_requests FOR SELECT
  TO anon, authenticated
  USING (true);

GRANT SELECT, INSERT, UPDATE ON public.results_heal_requests TO anon, authenticated;
GRANT ALL ON public.results_heal_requests TO service_role;

COMMENT ON TABLE public.results_heal_requests IS
  'User/tab-triggered refill of financial_results from exchange XBRL. Dislike with a comment sets force=true.';
