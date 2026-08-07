-- Toggleable thumbs + public counts for content_feedback.
-- Run in Supabase SQL Editor (safe if add_content_feedback.sql already ran).

-- One vote per visitor per section (enables upsert / unselect).
UPDATE public.content_feedback
  SET visitor_id = 'anon_' || id::text
  WHERE visitor_id IS NULL OR trim(visitor_id) = '';

ALTER TABLE public.content_feedback
  ALTER COLUMN visitor_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_content_feedback_visitor_section
  ON public.content_feedback (symbol, content_type, section_key, visitor_id);

-- Counts only (no comments) — safe for anon.
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

-- Allow update/delete of own visitor vote (visitor_id is client-supplied).
DO $$
BEGIN
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
        AND char_length(trim(symbol)) BETWEEN 1 AND 20
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

  -- Soften insert: downvotes may omit comment on re-vote; UI still asks once.
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
END $$;

GRANT INSERT, UPDATE, DELETE ON public.content_feedback TO anon, authenticated;
GRANT SELECT ON public.content_feedback TO authenticated;
