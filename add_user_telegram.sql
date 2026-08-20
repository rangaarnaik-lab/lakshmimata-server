-- Per-user Telegram bot chats + a public bot username the app can read.
-- Run in Supabase SQL editor, then set Railway env (see TELEGRAM_SETUP.md).

create table if not exists public.app_settings (
  key         text primary key,
  value       text not null default '',
  updated_at  timestamptz not null default now()
);

alter table public.app_settings enable row level security;

drop policy if exists "anyone can read app settings" on public.app_settings;
create policy "anyone can read app settings"
  on public.app_settings for select
  using (true);

create table if not exists public.user_telegram (
  user_id              uuid primary key references auth.users(id) on delete cascade,
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

alter table public.user_telegram enable row level security;

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

-- Authenticated users may rotate the link code / toggle enabled.
-- They must not overwrite chat_id (that is set by the scanner after /start).
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

notify pgrst, 'reload schema';
