-- PEAD (Post-Earnings Announcement Drift) and CANSLIM strategy tags
-- on the live stocks snapshot. Run in Supabase SQL Editor, then:
--   NOTIFY pgrst, 'reload schema';

ALTER TABLE public.stocks
  ADD COLUMN IF NOT EXISTS is_pead boolean,
  ADD COLUMN IF NOT EXISTS days_since_results int,
  ADD COLUMN IF NOT EXISTS last_results_date date,
  ADD COLUMN IF NOT EXISTS is_canslim boolean,
  ADD COLUMN IF NOT EXISTS canslim_score int,
  ADD COLUMN IF NOT EXISTS canslim_flags text;

NOTIFY pgrst, 'reload schema';
