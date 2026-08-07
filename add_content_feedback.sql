-- User thumbs up/down on AI content sections (About / Concall / PPT).
-- Supports select / unselect (one vote per visitor per section) + public counts.
-- Run in Supabase SQL Editor. If table already exists, also run update_content_feedback_votes.sql.

CREATE TABLE IF NOT EXISTS public.content_feedback (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  symbol text NOT NULL,
  content_type text NOT NULL,   -- about | concall | ppt | fundamentals | other
  section_key text NOT NULL,    -- overall_brief | customers | business_segments | …
  section_label text,
  vote text NOT NULL CHECK (vote IN ('up', 'down')),
  comment text,
  visitor_id text NOT NULL,
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

CREATE UNIQUE INDEX IF NOT EXISTS uq_content_feedback_visitor_section
  ON public.content_feedback (symbol, content_type, section_key, visitor_id);

ALTER TABLE public.content_feedback ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'content_feedback' AND policyname = 'content_feedback_public_insert'
  ) THEN
    DROP POLICY content_feedback_public_insert ON public.content_feedback;
  END IF;

  CREATE POLICY content_feedback_public_insert
    ON public.content_feedback FOR INSERT
    TO anon, authenticated
    WITH CHECK (
      vote IN ('up', 'down')
      AND char_length(trim(symbol)) BETWEEN 1 AND 20
      AND char_length(trim(content_type)) BETWEEN 1 AND 40
      AND char_length(trim(section_key)) BETWEEN 1 AND 60
      AND visitor_id IS NOT NULL
      AND char_length(visitor_id) >= 8
      AND (
        vote = 'up'
        OR comment IS NULL
        OR char_length(trim(comment)) BETWEEN 5 AND 1000
      )
    );

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'content_feedback' AND policyname = 'content_feedback_public_update'
  ) THEN
    CREATE POLICY content_feedback_public_update
      ON public.content_feedback FOR UPDATE
      TO anon, authenticated
      USING (visitor_id IS NOT NULL AND char_length(visitor_id) >= 8)
      WITH CHECK (
        vote IN ('up', 'down')
        AND (
          vote = 'up'
          OR comment IS NULL
          OR char_length(trim(comment)) BETWEEN 5 AND 1000
        )
      );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'content_feedback' AND policyname = 'content_feedback_public_delete'
  ) THEN
    CREATE POLICY content_feedback_public_delete
      ON public.content_feedback FOR DELETE
      TO anon, authenticated
      USING (visitor_id IS NOT NULL AND char_length(visitor_id) >= 8);
  END IF;

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

CREATE OR REPLACE FUNCTION public.get_content_feedback_counts(
  p_symbol text,
  p_content_type text,
  p_section_key text DEFAULT NULL
)
RETURNS TABLE(section_key text, up_count bigint, down_count bigint)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT
    cf.section_key,
    count(*) FILTER (WHERE cf.vote = 'up')::bigint AS up_count,
    count(*) FILTER (WHERE cf.vote = 'down')::bigint AS down_count
  FROM public.content_feedback cf
  WHERE cf.symbol = upper(trim(p_symbol))
    AND cf.content_type = lower(trim(p_content_type))
    AND (p_section_key IS NULL OR cf.section_key = p_section_key)
  GROUP BY cf.section_key;
$$;

GRANT EXECUTE ON FUNCTION public.get_content_feedback_counts(text, text, text)
  TO anon, authenticated;

GRANT INSERT, UPDATE, DELETE ON public.content_feedback TO anon, authenticated;
GRANT SELECT ON public.content_feedback TO authenticated;
GRANT ALL ON public.content_feedback TO service_role;
