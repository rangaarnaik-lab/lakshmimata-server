-- Per-user Telegram chats + public bot username.
--
-- If you see "deadlock detected": do NOT rerun this whole file in one go.
-- Run BLOCK 1, then BLOCK 2, then BLOCK 3 separately (each as its own Run).
-- Wait ~10s between blocks. Auth is using auth.users at the same time;
-- a long transaction that also locks that table is what deadlocked.
--
-- lock_timeout makes a blocked statement fail fast instead of hanging.

-- ══════════════════════════════════════════════════════════════════════
-- BLOCK 1 — tables only (no trigger, no FK on auth.users)
-- ══════════════════════════════════════════════════════════════════════
set lock_timeout = '8s';
set statement_timeout = '60s';

create table if not exists public.app_settings (
  key         text primary key,
  value       text not null default '',
  updated_at  timestamptz not null default now()
);

create table if not exists public.user_telegram (
  user_id              uuid primary key,
  chat_id              text,
  telegram_username    text,
  enabled              boolean not null default true,
  link_code            text,
  link_code_expires_at timestamptz,
  linked_at            timestamptz,
  updated_at           timestamptz not null default now()
);

create unique index if not exists user_telegram_chat_id_uidx
  on public.user_telegram (chat_id)
  where chat_id is not null;

create unique index if not exists user_telegram_link_code_uidx
  on public.user_telegram (link_code)
  where link_code is not null;

-- ══════════════════════════════════════════════════════════════════════
-- BLOCK 2 — RLS (short exclusive locks on the new tables only)
-- ══════════════════════════════════════════════════════════════════════
alter table public.app_settings enable row level security;
alter table public.user_telegram enable row level security;

drop policy if exists "anyone can read app settings" on public.app_settings;
create policy "anyone can read app settings"
  on public.app_settings for select
  using (true);

drop policy if exists "users read own telegram" on public.user_telegram;
create policy "users read own telegram"
  on public.user_telegram for select
  using (auth.uid() = user_id);

drop policy if exists "users insert own telegram" on public.user_telegram;
create policy "users insert own telegram"
  on public.user_telegram for insert
  with check (auth.uid() = user_id);

drop policy if exists "users update own telegram" on public.user_telegram;
create policy "users update own telegram"
  on public.user_telegram for update
  using (auth.uid() = user_id);

-- ══════════════════════════════════════════════════════════════════════
-- BLOCK 3 — trigger + optional FK (FK is the lock that hit auth.users)
-- ══════════════════════════════════════════════════════════════════════
create or replace function public.protect_user_telegram_chat()
returns trigger
language plpgsql
as $$
begin
  if auth.role() = 'authenticated' then
    if tg_op = 'UPDATE' then
      new.chat_id := old.chat_id;
      new.telegram_username := old.telegram_username;
      new.linked_at := old.linked_at;
    elsif tg_op = 'INSERT' then
      new.chat_id := null;
      new.telegram_username := null;
      new.linked_at := null;
    end if;
  end if;
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists trg_protect_user_telegram_chat on public.user_telegram;
create trigger trg_protect_user_telegram_chat
  before insert or update on public.user_telegram
  for each row execute procedure public.protect_user_telegram_chat();

-- Skip this statement if it deadlocks; the app still works without the FK.
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'user_telegram_user_id_fkey'
      and conrelid = 'public.user_telegram'::regclass
  ) then
    alter table public.user_telegram
      add constraint user_telegram_user_id_fkey
      foreign key (user_id) references auth.users(id) on delete cascade
      not valid;
    alter table public.user_telegram validate constraint user_telegram_user_id_fkey;
  end if;
exception
  when deadlock_detected or lock_not_available then
    raise notice 'FK skipped (deadlock/lock timeout). Safe to continue; retry this block later.';
end;
$$;

notify pgrst, 'reload schema';
