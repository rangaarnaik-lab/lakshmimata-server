-- Per-user Our Chart indicator parameters (which indicators are on,
-- plus Inputs / Style / Visibility for each). Restored on every login.
--
-- Run 014_subscription_gate.sql FIRST — the write policies below call
-- public.has_active_subscription().
--
-- Run the WHOLE file in Supabase -> SQL Editor (safe to re-run).
-- A table with RLS on and no INSERT policy causes:
--   new row violates row-level security policy for table "user_chart_indicator_prefs"

create table if not exists public.user_chart_indicator_prefs (
  user_id     uuid primary key references auth.users(id) on delete cascade,
  prefs       jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

alter table public.user_chart_indicator_prefs enable row level security;

grant usage on schema public to authenticated;
grant select, insert, update, delete on public.user_chart_indicator_prefs to authenticated;

-- Reads stay open to the owner even after a plan lapses: the settings are
-- still theirs, so renewing restores the layout instead of starting blank.
-- Saving is what costs money, so only the write policies carry the gate.
drop policy if exists "users read own chart indicator prefs" on public.user_chart_indicator_prefs;
create policy "users read own chart indicator prefs"
  on public.user_chart_indicator_prefs for select
  to authenticated
  using (auth.uid() = user_id);

drop policy if exists "users insert own chart indicator prefs" on public.user_chart_indicator_prefs;
create policy "users insert own chart indicator prefs"
  on public.user_chart_indicator_prefs for insert
  to authenticated
  with check (auth.uid() = user_id and public.has_active_subscription());

drop policy if exists "users update own chart indicator prefs" on public.user_chart_indicator_prefs;
create policy "users update own chart indicator prefs"
  on public.user_chart_indicator_prefs for update
  to authenticated
  using (auth.uid() = user_id and public.has_active_subscription())
  with check (auth.uid() = user_id and public.has_active_subscription());

drop policy if exists "users delete own chart indicator prefs" on public.user_chart_indicator_prefs;
create policy "users delete own chart indicator prefs"
  on public.user_chart_indicator_prefs for delete
  to authenticated
  using (auth.uid() = user_id);

-- PostgREST caches the schema; reload so upserts see the new policies.
notify pgrst, 'reload schema';
