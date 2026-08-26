-- Alerts must not carry raw market data.
--
-- squeeze_alerts is read by every signed-in browser (RLS is disabled and the
-- client selects *), so last_price / chg_pct meant our own Upstox feed was
-- being redistributed to all users. The fired signal, RS and sector are our
-- own computation and stay; live price now comes from each user's own broker.
--
-- Dropping rather than nulling: the columns are gone from the writer, so
-- keeping them would only preserve the historical quote values on old rows.

ALTER TABLE public.squeeze_alerts
  DROP COLUMN IF EXISTS last_price,
  DROP COLUMN IF EXISTS chg_pct;

COMMENT ON TABLE public.squeeze_alerts IS
  'Signal fires (squeeze, VCP, HY/HT, PP, Stage 2, Guppy, RS>70). Derived '
  'analytics only — no quotes. Live price comes from the user''s own broker.';
