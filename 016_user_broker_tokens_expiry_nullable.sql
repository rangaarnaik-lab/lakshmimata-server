-- An Upstox token's expiry is not always known at save time, so the column
-- must accept NULL. A NOT NULL constraint here rejected every OAuth connect.
ALTER TABLE public.user_broker_tokens
  ALTER COLUMN expires_at DROP NOT NULL;
