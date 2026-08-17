-- Per-user Our Chart indicator parameters (RSI length, MACD, Super Cycle, etc.).
-- Run this in Supabase → SQL Editor (NOT the .js file).

create table if not exists public.user_chart_indicator_prefs (
  user_id     uuid primary key references auth.users(id) on delete cascade,
  prefs       jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

alter table public.user_chart_indicator_prefs enable row level security;

drop policy if exists "users read own chart indicator prefs" on public.user_chart_indicator_prefs;
create policy "users read own chart indicator prefs"
  on public.user_chart_indicator_prefs for select
  using (auth.uid() = user_id);

drop policy if exists "users insert own chart indicator prefs" on public.user_chart_indicator_prefs;
create policy "users insert own chart indicator prefs"
  on public.user_chart_indicator_prefs for insert
  with check (auth.uid() = user_id);

drop policy if exists "users update own chart indicator prefs" on public.user_chart_indicator_prefs;
create policy "users update own chart indicator prefs"
  on public.user_chart_indicator_prefs for update
  using (auth.uid() = user_id);

drop policy if exists "users delete own chart indicator prefs" on public.user_chart_indicator_prefs;
create policy "users delete own chart indicator prefs"
  on public.user_chart_indicator_prefs for delete
  using (auth.uid() = user_id);
