-- Per-user broker credentials. access_token is AES-256-GCM ciphertext from the Vercel API.
CREATE TABLE IF NOT EXISTS public.user_broker_tokens (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  broker text NOT NULL CHECK (broker IN ('upstox', 'fyers')),
  access_token text NOT NULL,
  upstox_user_id text,
  expires_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS user_broker_tokens_user_broker_uidx
  ON public.user_broker_tokens (user_id, broker);

ALTER TABLE public.user_broker_tokens ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS user_broker_tokens_select_own ON public.user_broker_tokens;
CREATE POLICY user_broker_tokens_select_own
  ON public.user_broker_tokens FOR SELECT TO authenticated
  USING (auth.uid() = user_id);

DROP POLICY IF EXISTS user_broker_tokens_insert_own ON public.user_broker_tokens;
CREATE POLICY user_broker_tokens_insert_own
  ON public.user_broker_tokens FOR INSERT TO authenticated
  WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS user_broker_tokens_update_own ON public.user_broker_tokens;
CREATE POLICY user_broker_tokens_update_own
  ON public.user_broker_tokens FOR UPDATE TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS user_broker_tokens_delete_own ON public.user_broker_tokens;
CREATE POLICY user_broker_tokens_delete_own
  ON public.user_broker_tokens FOR DELETE TO authenticated
  USING (auth.uid() = user_id);

REVOKE ALL ON public.user_broker_tokens FROM anon;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.user_broker_tokens TO authenticated;
GRANT ALL ON public.user_broker_tokens TO service_role;
