-- One active device per account.
--
-- Supabase's signOut({scope:'others'}) only revokes other refresh tokens, so a
-- device that already holds an access token keeps working until that token
-- expires. This table records which auth session currently owns the account so
-- the replaced device can detect it immediately and sign itself out.
--
-- Run once in the Supabase SQL Editor. No scanner redeploy needed.

set lock_timeout = '8s';
set statement_timeout = '60s';

create table if not exists public.user_active_sessions (
  user_id      uuid primary key,
  session_id   uuid not null,
  activated_at timestamptz not null default now()
);

alter table public.user_active_sessions enable row level security;

-- Every device must be able to read the row to learn it was replaced, and to
-- claim ownership on a fresh sign-in. Both are scoped to the caller's own id.
drop policy if exists "users read own active session" on public.user_active_sessions;
create policy "users read own active session"
  on public.user_active_sessions for select
  using (auth.uid() = user_id);

drop policy if exists "users claim own active session" on public.user_active_sessions;
create policy "users claim own active session"
  on public.user_active_sessions for insert
  with check (auth.uid() = user_id);

drop policy if exists "users update own active session" on public.user_active_sessions;
create policy "users update own active session"
  on public.user_active_sessions for update
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Lets the replaced tab hear the takeover over Realtime instead of waiting for
-- its next poll. Safe to re-run: adding an existing table to the publication
-- raises duplicate_object, which is swallowed here.
do $$
begin
  alter publication supabase_realtime add table public.user_active_sessions;
exception
  when duplicate_object then null;
  when undefined_object then
    raise notice 'supabase_realtime publication not found — polling fallback still applies.';
end;
$$;

notify pgrst, 'reload schema';
