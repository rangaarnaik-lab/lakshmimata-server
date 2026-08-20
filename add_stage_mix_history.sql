-- Weinstein stage mix history (S1–S4) for the Market Overview trend chart.
-- Run once in Supabase SQL Editor, then redeploy the live scanner.

set lock_timeout = '8s';
set statement_timeout = '60s';

alter table public.ema_breadth_history
  add column if not exists stage1_count integer,
  add column if not exists stage3_count integer,
  add column if not exists stage4_count integer;

alter table public.market_breadth
  add column if not exists stage1_count integer,
  add column if not exists stage3_count integer;

-- Fill S1–S4 from archived stock_history on dates that already have an EMA row.
-- Does not insert new dates, so EMA-above % in the table stays intact.
update public.ema_breadth_history e
set
  stage1_count = s.s1,
  stage2_count = s.s2,
  stage3_count = s.s3,
  stage4_count = s.s4,
  total = coalesce(e.total, s.n)
from (
  select
    snapshot_date as d,
    count(*) filter (where weinstein_stage = 1) as s1,
    count(*) filter (where weinstein_stage = 2) as s2,
    count(*) filter (where weinstein_stage = 3) as s3,
    count(*) filter (where weinstein_stage = 4) as s4,
    count(*) as n
  from public.stock_history
  where weinstein_stage is not null
  group by snapshot_date
  having count(*) >= 50
) s
where e.date = s.d;

notify pgrst, 'reload schema';
