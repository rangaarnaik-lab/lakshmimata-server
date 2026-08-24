// Read-only health probe: when did each Gemini-backed feature last succeed?
// Uses the public anon key, so it only sees what the site itself can see.

const URL_BASE = process.env.SUPABASE_URL || 'https://fyhkxsaeylmhzzvmhgiu.supabase.co'
const ANON = process.env.SUPABASE_ANON_KEY || 'sb_publishable_68oG_98aHR0B5RPUAfLfig_TzF5ZwdK'
const H = { apikey: ANON, Authorization: `Bearer ${ANON}` }

async function q(path) {
  const r = await fetch(`${URL_BASE}/rest/v1/${path}`, { headers: H })
  const t = await r.text()
  if (!r.ok) return { err: `${r.status}: ${t.slice(0, 120)}` }
  try { return { rows: JSON.parse(t) } } catch { return { err: t.slice(0, 120) } }
}

const checks = [
  ['ask: last DONE', 'stock_ai_asks?status=eq.done&select=symbol,ask_mode,answered_at&order=answered_at.desc&limit=3'],
  ['ask: last ERROR', 'stock_ai_asks?status=eq.error&select=symbol,ask_mode,error,answered_at&order=answered_at.desc&limit=3'],
  ['mgmt flags', 'stock_fundamentals?select=sym,mgmt_flags_at&mgmt_flags_at=not.is.null&order=mgmt_flags_at.desc&limit=3'],
  ['about summaries', 'stock_fundamentals?select=sym,about_at&about_at=not.is.null&order=about_at.desc&limit=3'],
]

for (const [label, path] of checks) {
  const res = await q(path)
  console.log(`\n=== ${label} ===`)
  if (res.err) console.log('  n/a —', res.err)
  else if (!res.rows.length) console.log('  (none)')
  else for (const row of res.rows) console.log(' ', JSON.stringify(row).slice(0, 300))
}
