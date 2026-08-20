-- Per-user watchlists. Run once in Supabase SQL Editor.
-- Watchlists used to live only in the browser (localStorage), so they did
-- not follow the account to another phone and Telegram could not see them.

set lock_timeout = '8s';
set statement_timeout = '60s';

create table if not exists public.user_watchlists (
  user_id     uuid primary key,
  watchlists  jsonb not null default '[]'::jsonb,
  active_id   text,
  updated_at  timestamptz not null default now()
);

alter table public.user_watchlists enable row level security;

drop policy if exists "users read own watchlists" on public.user_watchlists;
create policy "users read own watchlists"
  on public.user_watchlists for select
  using (auth.uid() = user_id);

drop policy if exists "users insert own watchlists" on public.user_watchlists;
create policy "users insert own watchlists"
  on public.user_watchlists for insert
  with check (auth.uid() = user_id);

drop policy if exists "users update own watchlists" on public.user_watchlists;
create policy "users update own watchlists"
  on public.user_watchlists for update
  using (auth.uid() = user_id);

drop policy if exists "users delete own watchlists" on public.user_watchlists;
create policy "users delete own watchlists"
  on public.user_watchlists for delete
  using (auth.uid() = user_id);

do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'user_watchlists_user_id_fkey'
      and conrelid = 'public.user_watchlists'::regclass
  ) then
    alter table public.user_watchlists
      add constraint user_watchlists_user_id_fkey
      foreign key (user_id) references auth.users(id) on delete cascade
      not valid;
    alter table public.user_watchlists validate constraint user_watchlists_user_id_fkey;
  end if;
exception
  when deadlock_detected or lock_not_available then
    raise notice 'FK skipped (lock). Table and RLS still work; retry this block later.';
end;
$$;

notify pgrst, 'reload schema';
