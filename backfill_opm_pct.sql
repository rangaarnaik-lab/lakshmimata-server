-- Backfill operating margin % when missing (production rating uses stored opm_pct only).
-- OPM = (PBT − other income) / sales × 100
UPDATE public.financial_results
SET opm_pct = round(((pbt - other_income) / sales * 100)::numeric, 2)
WHERE opm_pct IS NULL
  AND sales IS NOT NULL AND sales <> 0
  AND pbt IS NOT NULL
  AND other_income IS NOT NULL;

NOTIFY pgrst, 'reload schema';
