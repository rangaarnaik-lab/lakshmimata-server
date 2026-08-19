-- Per-user Our Chart indicator parameters (which indicators are on,
-- plus Inputs / Style / Visibility for each). Restored on every login.
-- Run this in Supabase → SQL Editor if the table is missing.

create table if not exists public.user_chart_indicator_prefs (
  user_id     uuid primary key references auth.users(id) on delete cascade,
  prefs       jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

alter table public.user_chart_indicator_prefs enable row level security;

grant select, insert, update, delete on public.user_chart_indicator_prefs to authenticated;

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
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

drop policy if exists "users delete own chart indicator prefs" on public.user_chart_indicator_prefs;
create policy "users delete own chart indicator prefs"
  on public.user_chart_indicator_prefs for delete
  using (auth.uid() = user_id);
