-- Market Leadership / Emerging Heat support.
-- Run once in Supabase SQL Editor, then redeploy the live scanner.

set lock_timeout = '8s';
set statement_timeout = '60s';

create table if not exists public.industries (
  industry text primary key,
  parent_sector text,
  avg_rs integer,
  rank integer,
  rank_history jsonb not null default '[]'::jsonb,
  rank_change integer,
  count integer not null default 0,
  pp_count integer not null default 0,
  improving integer not null default 0,
  advances_d numeric,
  advances_w numeric,
  advances_m numeric,
  last_updated timestamptz not null default now()
);

create index if not exists industries_rank_idx
  on public.industries (rank);
create index if not exists industries_parent_sector_idx
  on public.industries (parent_sector);

alter table public.industries enable row level security;

drop policy if exists "Public can read industries" on public.industries;
create policy "Public can read industries"
  on public.industries for select
  using (true);

grant select on public.industries to anon, authenticated;

notify pgrst, 'reload schema';
