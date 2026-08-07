-- Dual Ask modes: filings (local PPT/concall only) | web (Google Search + filings)
ALTER TABLE public.stock_ai_asks
  ADD COLUMN IF NOT EXISTS ask_mode text NOT NULL DEFAULT 'filings';

COMMENT ON COLUMN public.stock_ai_asks.ask_mode IS
  'filings = PPT/concall on file only; web = Gemini Google Search + filings context';
