-- Subscription gate for per-user cloud features.
--
-- One helper the RLS policies on every per-user table can call, so "must be
-- signed in AND paying" lives in a single place instead of being copied into
-- a dozen policies (and silently drifting).
--
-- Access counts as valid when the caller's subscriptions row is either
--   * trialing  and trial_end is still in the future, or
--   * active    and current_period_end is still in the future.
-- Anything else (cancelled, past_due, expired trial, no row at all) is out.
--
-- SECURITY DEFINER because a policy that reads public.subscriptions would
-- otherwise be evaluated under the caller's own RLS on that table; keeping
-- the lookup here also means one plan check per statement.
--
-- Run in Supabase -> SQL Editor. Safe to re-run.

create or replace function public.has_active_subscription(uid uuid default auth.uid())
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select exists (
    select 1
    from public.subscriptions s
    where s.user_id = uid
      and (
        (s.status = 'trialing' and s.trial_end is not null and s.trial_end > now())
        or
        (s.status = 'active' and s.current_period_end is not null and s.current_period_end > now())
      )
  );
$$;

revoke all on function public.has_active_subscription(uuid) from public;
grant execute on function public.has_active_subscription(uuid) to authenticated;

notify pgrst, 'reload schema';
