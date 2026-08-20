-- Ask AI can attach one compressed chart screenshot (JPEG base64).
-- Worker sends it to Gemini vision, then clears the column after answering
-- so the table does not keep large images.

ALTER TABLE public.stock_ai_asks
  ADD COLUMN IF NOT EXISTS chart_image text,
  ADD COLUMN IF NOT EXISTS chart_image_mime text;

COMMENT ON COLUMN public.stock_ai_asks.chart_image IS
  'Temporary JPEG/PNG base64 of a user-attached chart. Cleared after the worker answers.';
COMMENT ON COLUMN public.stock_ai_asks.chart_image_mime IS
  'image/jpeg or image/png for chart_image.';

-- Drop and recreate insert policy so an attached chart still counts as a
-- valid question even when the typed text is the short default caption.
DROP POLICY IF EXISTS stock_ai_asks_public_insert ON public.stock_ai_asks;
CREATE POLICY stock_ai_asks_public_insert
  ON public.stock_ai_asks FOR INSERT
  TO anon, authenticated
  WITH CHECK (
    status = 'pending'
    AND char_length(trim(symbol)) BETWEEN 1 AND 20
    AND char_length(trim(question)) BETWEEN 8 AND 400
    AND (
      chart_image IS NULL
      OR (
        char_length(chart_image) BETWEEN 800 AND 900000
        AND chart_image_mime IN ('image/jpeg', 'image/png')
      )
    )
  );
