-- Chart speed: single-call intraday bars (optional DB rollup to 3/5/15/30/60).
-- Replaces up to 5 PostgREST pages from the browser.
-- Run in Supabase → SQL Editor, then:
--   NOTIFY pgrst, 'reload schema';

create or replace function public.get_stock_intraday_bars(
  p_sym        text,
  p_days       int default 10,
  p_limit      int default 4500,
  p_interval_m int default 1
)
returns jsonb
language plpgsql
stable
security invoker
set search_path = public
as $$
declare
  v_sym   text := upper(trim(p_sym));
  v_days  int  := greatest(1, least(coalesce(p_days, 10), 30));
  v_lim   int  := greatest(100, least(coalesce(p_limit, 4500), 12000));
  v_iv    int  := greatest(1, coalesce(p_interval_m, 1));
  v_since timestamptz := now() - (v_days || ' days')::interval;
  v_out   jsonb;
begin
  if v_sym is null or v_sym = '' then
    return '[]'::jsonb;
  end if;

  if v_iv = 1 then
    -- Raw 1-minute bars (newest lim, returned oldest→newest)
    select coalesce(jsonb_agg(row_to_json(x)::jsonb order by x.ts), '[]'::jsonb)
    into v_out
    from (
      select ts, open, high, low, close, volume
      from (
        select ts, open, high, low, close, volume
        from public.stock_intraday_1m
        where sym = v_sym
          and ts >= v_since
        order by ts desc
        limit v_lim
      ) newest
      order by ts asc
    ) x;
  else
    -- Roll up 1m → Nm in Postgres (IST bucket aligned to interval)
    select coalesce(jsonb_agg(row_to_json(x)::jsonb order by x.ts), '[]'::jsonb)
    into v_out
    from (
      select
        min(ts) as ts,
        (array_agg(open order by ts))[1] as open,
        max(high) as high,
        min(low) as low,
        (array_agg(close order by ts desc))[1] as close,
        sum(volume)::bigint as volume
      from (
        select
          ts, open, high, low, close, volume,
          -- Floor to IST minute, then to interval bucket
          (
            date_trunc('day', ts at time zone 'Asia/Kolkata')
            + (
                floor(
                  (
                    extract(hour from ts at time zone 'Asia/Kolkata') * 60
                    + extract(minute from ts at time zone 'Asia/Kolkata')
                  ) / v_iv
                ) * v_iv
              ) * interval '1 minute'
          ) at time zone 'Asia/Kolkata' as bucket_ts
        from (
          select ts, open, high, low, close, volume
          from public.stock_intraday_1m
          where sym = v_sym
            and ts >= v_since
          order by ts desc
          limit v_lim
        ) raw
      ) b
      group by bucket_ts
      order by min(ts) asc
    ) x;
  end if;

  return coalesce(v_out, '[]'::jsonb);
end;
$$;

comment on function public.get_stock_intraday_bars(text, int, int, int) is
  'Our Chart: one-call 1m (or rolled) bars for a symbol. Prefer over paginated REST.';

grant execute on function public.get_stock_intraday_bars(text, int, int, int)
  to anon, authenticated;
