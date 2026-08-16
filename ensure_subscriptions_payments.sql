-- Subscriptions + payment audit for Lakshmimata checkout (Razorpay).
-- Run once in Supabase SQL Editor, then: NOTIFY pgrst, 'reload schema';

CREATE TABLE IF NOT EXISTS public.subscriptions (
  user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  status text NOT NULL DEFAULT 'trialing',
  trial_end timestamptz,
  plan_cycle text,
  current_period_end timestamptz,
  razorpay_payment_id text,
  razorpay_subscription_id text,
  updated_at timestamptz DEFAULT now(),
  created_at timestamptz DEFAULT now()
);

ALTER TABLE public.subscriptions
  ADD COLUMN IF NOT EXISTS plan_cycle text,
  ADD COLUMN IF NOT EXISTS razorpay_payment_id text,
  ADD COLUMN IF NOT EXISTS razorpay_subscription_id text,
  ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();

CREATE TABLE IF NOT EXISTS public.payment_orders (
  id bigserial PRIMARY KEY,
  user_id uuid REFERENCES auth.users(id) ON DELETE SET NULL,
  plan_cycle text NOT NULL,
  amount_inr numeric NOT NULL,
  currency text NOT NULL DEFAULT 'INR',
  razorpay_order_id text,
  razorpay_payment_id text,
  status text NOT NULL DEFAULT 'created',
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS payment_orders_user_idx ON public.payment_orders (user_id);
CREATE INDEX IF NOT EXISTS payment_orders_payment_idx ON public.payment_orders (razorpay_payment_id);

ALTER TABLE public.subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.payment_orders ENABLE ROW LEVEL SECURITY;

DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename = 'subscriptions' AND policyname = 'subscriptions_select_own'
  ) THEN
    CREATE POLICY subscriptions_select_own ON public.subscriptions
      FOR SELECT TO authenticated USING (auth.uid() = user_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename = 'subscriptions' AND policyname = 'subscriptions_upsert_own'
  ) THEN
    CREATE POLICY subscriptions_upsert_own ON public.subscriptions
      FOR ALL TO authenticated USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename = 'payment_orders' AND policyname = 'payment_orders_select_own'
  ) THEN
    CREATE POLICY payment_orders_select_own ON public.payment_orders
      FOR SELECT TO authenticated USING (auth.uid() = user_id);
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename = 'payment_orders' AND policyname = 'payment_orders_insert_own'
  ) THEN
    CREATE POLICY payment_orders_insert_own ON public.payment_orders
      FOR INSERT TO authenticated WITH CHECK (auth.uid() = user_id);
  END IF;
END $$;

GRANT SELECT, INSERT, UPDATE ON public.subscriptions TO authenticated;
GRANT SELECT, INSERT ON public.payment_orders TO authenticated;
GRANT USAGE, SELECT ON SEQUENCE public.payment_orders_id_seq TO authenticated;
GRANT ALL ON public.subscriptions TO service_role;
GRANT ALL ON public.payment_orders TO service_role;

NOTIFY pgrst, 'reload schema';
