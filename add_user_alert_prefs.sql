-- Run in Supabase SQL editor if migration wasn't applied.
-- Per-user alert enable/disable preferences.

create table if not exists public.user_alert_prefs (
  user_id     uuid primary key references auth.users(id) on delete cascade,
  prefs       jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

alter table public.user_alert_prefs enable row level security;

drop policy if exists "users read own alert prefs" on public.user_alert_prefs;
create policy "users read own alert prefs"
  on public.user_alert_prefs for select
  using (auth.uid() = user_id);

drop policy if exists "users insert own alert prefs" on public.user_alert_prefs;
create policy "users insert own alert prefs"
  on public.user_alert_prefs for insert
  with check (auth.uid() = user_id);

drop policy if exists "users update own alert prefs" on public.user_alert_prefs;
create policy "users update own alert prefs"
  on public.user_alert_prefs for update
  using (auth.uid() = user_id);

drop policy if exists "users delete own alert prefs" on public.user_alert_prefs;
create policy "users delete own alert prefs"
  on public.user_alert_prefs for delete
  using (auth.uid() = user_id);
