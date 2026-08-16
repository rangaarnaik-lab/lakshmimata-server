-- Near 5-day EMA pullback (same pattern as near_ema9 / near_ema21 / near_ema50)
alter table public.stocks add column if not exists near_ema5 boolean;
alter table public.stocks add column if not exists pct_from_ema5 numeric;
-- ema5 already exists from 52WL scanner
