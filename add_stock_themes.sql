-- Per-stock emerging themes (PPT/concall sync + AI web fill for all symbols).
-- Powers the Emerging Themes card under Market Cap on every chart.
ALTER TABLE public.stock_fundamentals
  ADD COLUMN IF NOT EXISTS emerging_themes jsonb,
  ADD COLUMN IF NOT EXISTS theme_evidence jsonb,
  ADD COLUMN IF NOT EXISTS theme_intensity text,
  ADD COLUMN IF NOT EXISTS themes_source text,
  ADD COLUMN IF NOT EXISTS themes_at timestamptz,
  ADD COLUMN IF NOT EXISTS themes_announced_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_stock_fundamentals_emerging_themes
  ON public.stock_fundamentals USING gin (emerging_themes);
