# Lakshmimata frontend deploy (patch workflow)

Production Vercel is wired to `rangaarnaik-lab/Lakshmimata` **main**, which Cursor cannot push to. This repo ships frontend fixes via `frontend-pead-canslim.patch` and a GitHub Actions workflow.

## What the patch includes

- PEAD / CANSLIM badges and filters
- **APOLLO-style result rating fix**: derives OPM from PBT − other income when `opm_pct` is null, caps margin compression and other-income spikes so QoQ recovery cannot promote back to Excellent

## Deploy to production

1. Ensure **lakshmimata-server** repo secrets are set: `VERCEL_TOKEN`, `VERCEL_PROJECT_ID` (and `VERCEL_TEAM_ID` if applicable — copy from Lakshmimata repo secrets).
2. Merge changes to `main` (patch or workflow), **or** run manually:
   - [Deploy Lakshmimata Frontend workflow](https://github.com/rangaarnaik-lab/lakshmimata-server/actions/workflows/deploy-lakshmimata-frontend.yml) → **Run workflow**

Merges that touch `deploy/frontend-pead-canslim.patch` or this workflow auto-trigger a production deploy.

## Regenerate the patch

```bash
cd Lakshmimata
git format-patch main..cursor/pead-canslim-tags-d9a9 --stdout > ../deploy/frontend-pead-canslim.patch
```
