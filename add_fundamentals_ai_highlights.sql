-- AI takeaways for the Fundamentals tab (important metrics narrative).
ALTER TABLE public.stock_fundamentals
  ADD COLUMN IF NOT EXISTS ai_highlights jsonb,
  ADD COLUMN IF NOT EXISTS ai_key_metrics jsonb,
  ADD COLUMN IF NOT EXISTS ai_highlights_at timestamptz;
