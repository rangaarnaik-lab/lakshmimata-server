-- 1-minute OHLCV bars for Our Chart (rollup to 3/5/15 on the client).
-- Built each live scan from Upstox quotes when ENABLE_INTRADAY_1M=1.
-- Keep ~30 trading days. Run in Supabase → SQL Editor, then:
--   NOTIFY pgrst, 'reload schema';

create table if not exists public.stock_intraday_1m (
  sym     text        not null,
  ts      timestamptz not null,
  open    numeric     not null,
  high    numeric     not null,
  low     numeric     not null,
  close   numeric     not null,
  volume  bigint      not null default 0,
  primary key (sym, ts)
);

create index if not exists stock_intraday_1m_sym_ts_desc_idx
  on public.stock_intraday_1m (sym, ts desc);

create index if not exists stock_intraday_1m_ts_idx
  on public.stock_intraday_1m (ts);

alter table public.stock_intraday_1m enable row level security;

drop policy if exists stock_intraday_1m_public_read on public.stock_intraday_1m;
create policy stock_intraday_1m_public_read
  on public.stock_intraday_1m
  for select
  to anon, authenticated
  using (true);

grant select on public.stock_intraday_1m to anon, authenticated;

-- Live feature flags for the frontend (hide 1/3/5/15 without redeploy).
alter table public.scan_meta
  add column if not exists features jsonb not null default '{}'::jsonb;

comment on table public.stock_intraday_1m is
  'Per-minute OHLCV from live Upstox quotes; chart rolls up to 3/5/15m. Gated by ENABLE_INTRADAY_1M.';
