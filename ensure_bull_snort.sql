-- Bull Snort signal on stocks (RS filter + scanner).
-- Run once in Supabase SQL Editor, then: NOTIFY pgrst, 'reload schema';

ALTER TABLE public.stocks
  ADD COLUMN IF NOT EXISTS is_bull_snort boolean,
  ADD COLUMN IF NOT EXISTS bull_snort_vol_ratio numeric;

NOTIFY pgrst, 'reload schema';
