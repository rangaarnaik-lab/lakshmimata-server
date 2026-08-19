-- Run in the Supabase SQL editor before the next scan.
--
-- Squeeze Pro (John Carter) columns. The scan already writes in_squeeze /
-- squeeze_fired / squeeze_days / bb_width_pct; these add the compression tier,
-- how long the coil has held and which side momentum is on:
--
--   sqz_level      none | low | mid | high   (BB inside the 2.0 / 1.5 / 1.0 ATR Keltner)
--   sqz_days       bars in any compression   -> the "long squeeze" number
--   sqz_high_days  bars at high compression
--   sqz_mom        TTM momentum; sign is the long/short side of the break
--   sqz_mom_slope  momentum change vs the prior bar (rising or fading)
--   sqz_bias       long | short
--   sqz_fired_dir  which way the coil released, on the bar it released
--
-- Unknown columns are silently dropped from the upsert, so until this runs the
-- scan keeps working and the UI falls back to the classic squeeze flag.

alter table public.stocks add column if not exists sqz_level      text;
alter table public.stocks add column if not exists sqz_days       integer default 0;
alter table public.stocks add column if not exists sqz_high_days  integer default 0;
alter table public.stocks add column if not exists sqz_mom        numeric;
alter table public.stocks add column if not exists sqz_mom_slope  numeric;
alter table public.stocks add column if not exists sqz_bias       text;
alter table public.stocks add column if not exists sqz_fired_dir  text;

-- Same columns on the EOD archive so replaying a past day shows the same tiers.
alter table public.stock_history add column if not exists sqz_level      text;
alter table public.stock_history add column if not exists sqz_days       integer default 0;
alter table public.stock_history add column if not exists sqz_high_days  integer default 0;
alter table public.stock_history add column if not exists sqz_mom        numeric;
alter table public.stock_history add column if not exists sqz_mom_slope  numeric;
alter table public.stock_history add column if not exists sqz_bias       text;
alter table public.stock_history add column if not exists sqz_fired_dir  text;

-- Scanner sorts on "hardest coil, longest held", so index the pair.
create index if not exists stocks_sqz_level_days_idx
  on public.stocks (sqz_level, sqz_days desc);

notify pgrst, 'reload schema';
