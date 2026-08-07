-- User thumbs up/down on AI content sections (About / Concall / PPT).
-- Thumbs-down rows include a free-text issue so we can fix prompts/data.
-- Run in Supabase SQL Editor.

CREATE TABLE IF NOT EXISTS public.content_feedback (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol text NOT NULL,
  content_type text NOT NULL,   -- about | concall | ppt | fundamentals | other
  section_key text NOT NULL,    -- overall_brief | customers | business_segments | …
  section_label text,
  vote text NOT NULL CHECK (vote IN ('up', 'down')),
  comment text,
  visitor_id text,
  user_id uuid,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_content_feedback_created
  ON public.content_feedback (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_content_feedback_symbol
  ON public.content_feedback (symbol, content_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_content_feedback_down
  ON public.content_feedback (created_at DESC)
  WHERE vote = 'down';

ALTER TABLE public.content_feedback ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'content_feedback' AND policyname = 'content_feedback_public_insert'
  ) THEN
    CREATE POLICY content_feedback_public_insert
      ON public.content_feedback FOR INSERT
      TO anon, authenticated
      WITH CHECK (
        vote IN ('up', 'down')
        AND char_length(trim(symbol)) BETWEEN 1 AND 20
        AND char_length(trim(content_type)) BETWEEN 1 AND 40
        AND char_length(trim(section_key)) BETWEEN 1 AND 60
        AND (
          vote = 'up'
          OR (comment IS NOT NULL AND char_length(trim(comment)) BETWEEN 5 AND 1000)
        )
      );
  END IF;

  -- Authenticated owners / service can read; anon insert-only by default.
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'content_feedback' AND policyname = 'content_feedback_auth_select'
  ) THEN
    CREATE POLICY content_feedback_auth_select
      ON public.content_feedback FOR SELECT
      TO authenticated
      USING (true);
  END IF;
END $$;

GRANT INSERT ON public.content_feedback TO anon, authenticated;
GRANT SELECT ON public.content_feedback TO authenticated;
GRANT ALL ON public.content_feedback TO service_role;
