-- ============================================================================
-- Stocks that are at / just set their 52-week low within the last 30 trading
-- days.
--
-- Source data : public.stock_full_history
--               (one row per stock; dates[], prices[] are JSONB arrays,
--                positions aligned; newest bar is the LAST element)
--
-- 52-week low (l52) : min(prices[-252:]) — same window as live_scan's l52/h52.
--                     Falls back to min(prices) for symbols with <252 closes.
--
-- The trailing window below uses the last 30 bars. A symbol "set a NEW 52-week
-- low in the window" when its trailing-30 minimum equals the trailing-252
-- minimum (i.e. the all-time low of the window actually happened in the last
-- 30 trading days). The final WHERE also keeps any symbol sitting within 0% of
-- its 52-week low right now.
--
-- To change the window / tolerance, edit the two constants (n_days, near_pct)
-- in the `cfg` CTE near the top of the query.
-- ============================================================================

with
-- Constants you can edit.
cfg as (select 30 as n_days, 0.0 as near_pct),

-- Flatten each symbol's date/price arrays into ordered rows, newest last.
raw as (
  select s.sym,
         d.dt::text  as dt,
         p.price::numeric as price,
         p.ord - 1 as rev,                 -- 0 = oldest, N-1 = newest
         (select count(*)::int from jsonb_array_elements_text(s.prices)) as n
  from public.stock_full_history s
       cross join lateral jsonb_array_elements_text(s.prices) with ordinality p(price, ord)
       cross join lateral jsonb_array_elements_text(s.dates)  with ordinality d(dt, dord)
  where p.ord = d.dord
),
-- Trailing-252 minimum and trailing-N minimum per symbol.
windows as (
  select r.sym,
         r.n,
         min(r.price) as l52,                  -- trailing-252 minimum
         min(r.price) filter (where r.rev >= r.n - c.n_days) as tail_min,  -- trailing-N minimum
         bool_and(r.n >= 252) as full_hist
  from raw r, cfg c
  group by r.sym, r.n
),
-- Latest close per symbol.
latest as (
  select distinct on (sym)
         sym, price as last_close, dt as last_dt, n
  from raw
  order by sym, rev desc
),
-- Low bar per symbol: lowest price, and among ties the most recent bar.
lowbar as (
  select distinct on (sym)
         sym, price as low_close, dt as low_dt, rev as low_rev, n
  from raw
  order by sym, price asc, rev desc
)
select w.sym,
       lb.low_dt                        as low_date,
       round(lb.low_close, 2)           as low_close,
       round(lt.last_close, 2)          as latest_close,
       round((lt.last_close / lb.low_close - 1) * 100, 2) as pct_from_52wl,
       (w.tail_min <= lb.low_close)     as new_52w_low,
       (w.n - 1 - lb.low_rev)           as bars_since_low,   -- 0 = low was the latest bar
       case when w.full_hist then 'full' else 'short' end  as history
from windows w
join cfg c on true
join latest lt on lt.sym = w.sym
join lowbar  lb on lb.sym = w.sym
where lb.low_rev >= w.n - c.n_days                              -- low bar within last N days
   or (lt.last_close / lb.low_close - 1) * 100 <= c.near_pct    -- or close within NEAR_PCT% of low
order by new_52w_low desc, lb.low_rev desc, pct_from_52wl asc;