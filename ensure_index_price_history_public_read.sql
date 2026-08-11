-- Allow the frontend (anon / authenticated) to read index chart history.
-- Without this, Our Chart for indices returns empty and looks broken.
-- Safe to re-run.

ALTER TABLE public.index_price_history ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'index_price_history'
      AND policyname = 'index_price_history_public_read'
  ) THEN
    CREATE POLICY index_price_history_public_read
      ON public.index_price_history
      FOR SELECT
      TO anon, authenticated
      USING (true);
  END IF;
END $$;

GRANT SELECT ON public.index_price_history TO anon, authenticated;
