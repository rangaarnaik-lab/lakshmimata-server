-- Keep Ask AI follow-up questions in one conversation.
ALTER TABLE public.stock_ai_asks
  ADD COLUMN IF NOT EXISTS conversation_id uuid,
  ADD COLUMN IF NOT EXISTS parent_ask_id uuid REFERENCES public.stock_ai_asks(id) ON DELETE SET NULL;

UPDATE public.stock_ai_asks
SET conversation_id = id
WHERE conversation_id IS NULL;

ALTER TABLE public.stock_ai_asks
  ALTER COLUMN conversation_id SET DEFAULT gen_random_uuid(),
  ALTER COLUMN conversation_id SET NOT NULL;

-- Recreate the ask_mode check so chart screenshots can queue.
-- Drop first, then map any leftover values (web_search, empty, etc.)
-- onto filings/web/chart before putting the check back.
ALTER TABLE public.stock_ai_asks
  DROP CONSTRAINT IF EXISTS stock_ai_asks_ask_mode_check;

UPDATE public.stock_ai_asks
SET ask_mode = CASE
  WHEN lower(trim(coalesce(ask_mode, ''))) IN ('web', 'web_search', 'search', 'news') THEN 'web'
  WHEN lower(trim(coalesce(ask_mode, ''))) IN ('chart', 'vision', 'image') THEN 'chart'
  ELSE 'filings'
END
WHERE ask_mode IS NULL
   OR lower(trim(ask_mode)) NOT IN ('filings', 'web', 'chart');

ALTER TABLE public.stock_ai_asks
  ADD CONSTRAINT stock_ai_asks_ask_mode_check
  CHECK (ask_mode IN ('filings', 'web', 'chart'));

CREATE INDEX IF NOT EXISTS idx_stock_ai_asks_conversation
  ON public.stock_ai_asks (conversation_id, created_at ASC);

COMMENT ON COLUMN public.stock_ai_asks.conversation_id IS
  'Groups follow-up Ask AI questions into one chat.';
COMMENT ON COLUMN public.stock_ai_asks.parent_ask_id IS
  'Previous question in the chat, used to preserve follow-up context.';
