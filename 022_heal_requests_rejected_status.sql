-- Allow the EOD job to mark a dislike as not genuine ("rejected") when the
-- data it complained about is already complete and fresh. Safe to re-run.

ALTER TABLE public.fundamentals_heal_requests
  DROP CONSTRAINT IF EXISTS fundamentals_heal_requests_status_check;

ALTER TABLE public.fundamentals_heal_requests
  ADD CONSTRAINT fundamentals_heal_requests_status_check
  CHECK (status IN ('pending', 'working', 'done', 'failed', 'rejected'));

ALTER TABLE public.results_heal_requests
  DROP CONSTRAINT IF EXISTS results_heal_requests_status_check;

ALTER TABLE public.results_heal_requests
  ADD CONSTRAINT results_heal_requests_status_check
  CHECK (status IN ('pending', 'working', 'done', 'failed', 'rejected'));

-- The worker deletes down-votes it proved wrong, so give it that permission.
GRANT DELETE ON public.content_feedback TO service_role;

COMMENT ON COLUMN public.fundamentals_heal_requests.status IS
  'rejected = dislike checked against the stored ratios and found unnecessary.';
COMMENT ON COLUMN public.results_heal_requests.status IS
  'rejected = dislike checked against stored quarters and found unnecessary.';
