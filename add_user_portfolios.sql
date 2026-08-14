-- Run in Supabase SQL editor if migration wasn't applied.
-- Per-user family portfolios (max 5) + holdings JSON.

create table if not exists public.user_portfolios (
  user_id     uuid primary key references auth.users(id) on delete cascade,
  portfolios  jsonb not null default '[]'::jsonb,
  active_id   text,
  updated_at  timestamptz not null default now()
);

alter table public.user_portfolios enable row level security;

drop policy if exists "users read own portfolios" on public.user_portfolios;
create policy "users read own portfolios"
  on public.user_portfolios for select
  using (auth.uid() = user_id);

drop policy if exists "users insert own portfolios" on public.user_portfolios;
create policy "users insert own portfolios"
  on public.user_portfolios for insert
  with check (auth.uid() = user_id);

drop policy if exists "users update own portfolios" on public.user_portfolios;
create policy "users update own portfolios"
  on public.user_portfolios for update
  using (auth.uid() = user_id);

drop policy if exists "users delete own portfolios" on public.user_portfolios;
create policy "users delete own portfolios"
  on public.user_portfolios for delete
  using (auth.uid() = user_id);
