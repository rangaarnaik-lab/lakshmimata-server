# Lakshmimata frontend deploy (patch workflow)

Production Vercel is wired to `rangaarnaik-lab/Lakshmimata` **main**, which Cursor cannot push to. This repo ships frontend fixes via `frontend-pead-canslim.patch` and a GitHub Actions workflow.

## What the patch includes

- PEAD / CANSLIM badges and filters
- **APOLLO-style result rating fix**: derives OPM from PBT − other income when `opm_pct` is null, caps margin compression and other-income spikes so QoQ recovery cannot promote back to Excellent

## Deploy to production

### Option A — GitHub Pages (no Vercel secrets)

1. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions** (one-time).
2. Run [Deploy Lakshmimata Frontend](https://github.com/rangaarnaik-lab/lakshmimata-server/actions/workflows/deploy-lakshmimata-frontend.yml) or merge to `main`.

The patched app is published to GitHub Pages even when Vercel secrets are missing.

### Option B — Vercel (pocketrs-pro.vercel.app)

Add `VERCEL_TOKEN` and `VERCEL_PROJECT_ID` to [lakshmimata-server secrets](https://github.com/rangaarnaik-lab/lakshmimata-server/settings/secrets/actions). The same workflow deploys to Vercel when those are set.

## Database backfill (no frontend deploy)

If `opm_pct` is null, production rates strong YoY as Excellent (e.g. APOLLO). Run `backfill_opm_pct.sql` in Supabase or `python3 scripts/backfill_opm_pct.py` — caps those cases to **Good** immediately on the live Vercel app.

## Regenerate the patch

```bash
cd Lakshmimata
git format-patch main..cursor/pead-canslim-tags-d9a9 --stdout > ../deploy/frontend-pead-canslim.patch
```
