-- Cap genuine issue reports at 5 down-votes per person per IST day.
-- Updating an existing down-vote (same section) does not consume another slot.
-- Safe to re-run.

CREATE OR REPLACE FUNCTION public.ist_day_start(p_at timestamptz DEFAULT now())
RETURNS timestamptz
LANGUAGE sql
STABLE
AS $$
  SELECT ((p_at AT TIME ZONE 'Asia/Kolkata')::date)::timestamp
         AT TIME ZONE 'Asia/Kolkata';
$$;

CREATE OR REPLACE FUNCTION public.count_today_content_dislikes(
  p_visitor_id text,
  p_user_id uuid DEFAULT NULL,
  p_exclude_symbol text DEFAULT NULL,
  p_exclude_content_type text DEFAULT NULL,
  p_exclude_section_key text DEFAULT NULL
)
RETURNS integer
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
  SELECT count(*)::integer
  FROM public.content_feedback cf
  WHERE cf.vote = 'down'
    AND cf.created_at >= public.ist_day_start(now())
    AND (
      (p_user_id IS NOT NULL AND cf.user_id = p_user_id)
      OR (p_visitor_id IS NOT NULL AND cf.visitor_id = p_visitor_id)
    )
    AND NOT (
      p_exclude_symbol IS NOT NULL
      AND cf.symbol = upper(trim(p_exclude_symbol))
      AND cf.content_type = lower(trim(p_exclude_content_type))
      AND cf.section_key = p_exclude_section_key
    );
$$;

CREATE OR REPLACE FUNCTION public.content_feedback_dislike_daily_limit()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  n integer;
  day_start timestamptz;
BEGIN
  IF NEW.vote IS DISTINCT FROM 'down' THEN
    RETURN NEW;
  END IF;
  -- Same-section down-vote being edited (comment / kind) is not a new report.
  IF TG_OP = 'UPDATE' AND OLD.vote = 'down' THEN
    RETURN NEW;
  END IF;

  -- Treat an up→down flip as a report filed today.
  IF TG_OP = 'UPDATE' AND OLD.vote IS DISTINCT FROM 'down' THEN
    NEW.created_at := now();
  END IF;

  day_start := public.ist_day_start(now());
  SELECT count(*)::integer INTO n
  FROM public.content_feedback cf
  WHERE cf.vote = 'down'
    AND cf.created_at >= day_start
    AND (
      (NEW.user_id IS NOT NULL AND cf.user_id = NEW.user_id)
      OR (NEW.visitor_id IS NOT NULL AND cf.visitor_id = NEW.visitor_id)
    )
    AND (TG_OP = 'INSERT' OR cf.id <> OLD.id);

  IF n >= 5 THEN
    RAISE EXCEPTION 'You can report at most 5 issues per day'
      USING ERRCODE = 'P0001';
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_content_feedback_dislike_daily_limit
  ON public.content_feedback;
CREATE TRIGGER trg_content_feedback_dislike_daily_limit
  BEFORE INSERT OR UPDATE ON public.content_feedback
  FOR EACH ROW
  EXECUTE PROCEDURE public.content_feedback_dislike_daily_limit();

GRANT EXECUTE ON FUNCTION public.count_today_content_dislikes(text, uuid, text, text, text)
  TO anon, authenticated;
GRANT EXECUTE ON FUNCTION public.ist_day_start(timestamptz)
  TO anon, authenticated, service_role;
