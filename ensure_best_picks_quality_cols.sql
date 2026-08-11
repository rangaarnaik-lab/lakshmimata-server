-- AI Best Picks quality pillars — store RS/stage companions already present,
-- plus fundamental score/label, result rating, and Stage-2-new flag.
-- Safe to re-run.

ALTER TABLE public.best_picks
  ADD COLUMN IF NOT EXISTS fundamental_score numeric,
  ADD COLUMN IF NOT EXISTS fundamental_label text,
  ADD COLUMN IF NOT EXISTS result_rating text,
  ADD COLUMN IF NOT EXISTS is_s2_new_entry boolean DEFAULT false;

-- Public read already expected on best_picks; grant is idempotent.
GRANT SELECT ON public.best_picks TO anon, authenticated;
