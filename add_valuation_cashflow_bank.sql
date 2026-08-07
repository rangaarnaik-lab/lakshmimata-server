-- Valuation, cash-flow, and bank-specific ratios.
-- Run once in Supabase SQL editor.

-- Snapshot store (fundamentals worker / Screener scrape)
ALTER TABLE public.stock_fundamentals
  ADD COLUMN IF NOT EXISTS pb numeric,
  ADD COLUMN IF NOT EXISTS roce numeric,
  ADD COLUMN IF NOT EXISTS industry_pe numeric,
  ADD COLUMN IF NOT EXISTS div_yield numeric,
  ADD COLUMN IF NOT EXISTS cfo numeric,
  ADD COLUMN IF NOT EXISTS fcf numeric,
  ADD COLUMN IF NOT EXISTS cfo_pat numeric,
  ADD COLUMN IF NOT EXISTS nim numeric,
  ADD COLUMN IF NOT EXISTS gnpa numeric,
  ADD COLUMN IF NOT EXISTS nnpa numeric,
  ADD COLUMN IF NOT EXISTS car numeric,
  ADD COLUMN IF NOT EXISTS casa numeric;

-- Live stocks table (copied from fundamentals_cache each scan)
ALTER TABLE public.stocks
  ADD COLUMN IF NOT EXISTS pb numeric,
  ADD COLUMN IF NOT EXISTS roce numeric,
  ADD COLUMN IF NOT EXISTS industry_pe numeric,
  ADD COLUMN IF NOT EXISTS div_yield numeric,
  ADD COLUMN IF NOT EXISTS cfo numeric,
  ADD COLUMN IF NOT EXISTS fcf numeric,
  ADD COLUMN IF NOT EXISTS cfo_pat numeric,
  ADD COLUMN IF NOT EXISTS nim numeric,
  ADD COLUMN IF NOT EXISTS gnpa numeric,
  ADD COLUMN IF NOT EXISTS nnpa numeric,
  ADD COLUMN IF NOT EXISTS car numeric,
  ADD COLUMN IF NOT EXISTS casa numeric;
