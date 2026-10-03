-- ============================================================================
-- Stocks that crossed ABOVE their 50-day EMA in the last 2 trading days,
-- where that cross day was ALSO a pocket pivot, plus the return since the cross.
--
-- Source data : public.stock_full_history
--               (one row per stock; dates[], prices[], volumes[] are JSONB arrays)
--
-- Pocket pivot = UP day (close > prev close),
--                price ABOVE its 10-day & 50-day SMA,
--                within 8% above the 10-day SMA,
--                and volume BEATS the worst down-day volume of the prior 10 bars.
--
-- EMA(50) method (same as live_scan.ema_arr):
--   seed:  ema50 at bar 50 = SMA of first 50 closes
--   then:  ema[i] = alpha * price[i] + (1 - alpha) * ema[i-1],
--          alpha = 2/51 = 0.0392156863
--
-- Return% = (latest close / cross-day close) - 1
--   bars_since = 0  -> crossed today  (return % is ~0 by construction)
--   bars_since = 1  -> crossed yesterday
-- ============================================================================

with recursive
-- Flatten each stock's JSONB arrays into ordered rows (price, volume, date).
raw as (
  select s.sym,
         p.ord,
         p.price::numeric as price,
         v.vol::numeric    as vol,
         d.dt::text        as dt,
         (select count(*)::int from jsonb_array_elements_text(s.prices)) as cnt
  from public.stock_full_history s
       cross join lateral jsonb_array_elements_text(s.prices)  with ordinality p(price, ord)
       cross join lateral jsonb_array_elements_text(s.dates)   with ordinality d(dt,   do)
       cross join lateral jsonb_array_elements_text(s.volumes) with ordinality v(vol,  vo)
  where p.ord = d.do and p.ord = v.vo
),
-- Iterative EMA(50) series.
ema as (
  -- seed: EMA50 at bar 50 = SMA of the first 50 closes
  select sym, ord, price, vol, dt, cnt,
         (select avg(x.price) from raw x where x.sym = r.sym and x.ord <= 50) as ema50
  from raw r
  where r.ord = 50

  union all

  -- recurse: ema[i] = alpha*price[i] + (1-alpha)*ema[i-1]
  select r.sym, r.ord, r.price, r.vol, r.dt, r.cnt,
         round((0.0392156863 * r.price) + (0.9607843137 * e.ema50), 4)
  from raw r
  join ema e on e.sym = r.sym and e.ord = r.ord - 1
  where r.ord > 50
),
-- Add previous close / previous EMA and moving averages.
annotated as (
  select sym, ord, dt, cnt, price, vol, ema50,
         lag(price) over w as prev_price,
         lag(ema50) over w as prev_ema,
         cnt - ord as bars_since,                    -- 0 = today, 1 = yesterday
         avg(price) over (w rows between 9  preceding and current row) as ma10,
         avg(price) over (w rows between 49 preceding and current row) as ma50
  from ema
  window w as (partition by sym order by ord)
),
-- Recent cross-Above the 50-day EMA.
crosses as (
  select a.*,
         (select av.price from annotated av
           where av.sym = a.sym order by av.ord desc limit 1) as latest
  from annotated a
  where a.prev_price is not null and a.prev_ema is not null
    and a.prev_price <= a.prev_ema      -- previous close at/below EMA50
    and a.price > a.ema50               -- this close above EMA50  => cross-above
    and a.bars_since <= 1               -- within the last 2 trading days
),
-- Worst down-day volume of the 10 bars BEFORE the cross bar.
max_down as (
  select c.sym, c.ord,
         coalesce(
           (select max(vv.vol) from annotated vv
             where vv.sym = c.sym and vv.ord between c.ord-10 and c.ord-1
               and vv.price < vv.prev_price),
           (select avg(vv.vol) from annotated vv       -- fallback: avg when no down-days
             where vv.sym = c.sym and vv.ord between c.ord-10 and c.ord-1)
         ) as down_vol
  from crosses c
)
select c.sym,
       c.dt as cross_date,
       c.bars_since,
       round(c.price, 2)                     as cross_close,
       round(c.latest, 2)                    as latest_close,
       round(((c.latest / c.price) - 1) * 100, 2) as return_pct
from crosses c
join max_down m on m.sym = c.sym and m.ord = c.ord
where c.price > c.prev_price                       -- up day (pocket pivot rule 1)
  and c.price > c.ma10 and c.price < c.ma10 * 1.08 -- within 8% above 10-day SMA
  and c.price > c.ma50                             -- above 50-day SMA
  and c.vol > m.down_vol                           -- volume beats prior worst down-day
order by return_pct desc;