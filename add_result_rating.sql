-- Result quality rating written by the fundamentals worker while reading
-- Results PDFs (Excellent / Good / Neutral / Weak). Run in Supabase SQL editor.

ALTER TABLE public.financial_results
  ADD COLUMN IF NOT EXISTS result_rating text;

ALTER TABLE public.financial_results
  ADD COLUMN IF NOT EXISTS result_rating_note text;

ALTER TABLE public.concall_summaries
  ADD COLUMN IF NOT EXISTS result_rating text;

ALTER TABLE public.concall_summaries
  ADD COLUMN IF NOT EXISTS result_rating_note text;

COMMENT ON COLUMN public.financial_results.result_rating IS
  'Latest-period quality label: Excellent|Good|Neutral|Weak — set during Results PDF / YoY enrichment';

COMMENT ON COLUMN public.concall_summaries.result_rating IS
  'Result quality for this filing PDF — mirrors financial_results.result_rating';
