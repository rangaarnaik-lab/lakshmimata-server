// Diagnostic probe for the Ask AI queue.
// Inserts a pending question (optionally with a generated chart PNG) using the
// public anon key, then polls until the worker answers, errors, or times out.
//
//   node tools/ask_ai_probe.mjs SYMBOL [--chart] [--mode filings|web|chart]

import zlib from 'node:zlib'

const URL_BASE = process.env.SUPABASE_URL || 'https://fyhkxsaeylmhzzvmhgiu.supabase.co'
const ANON = process.env.SUPABASE_ANON_KEY || 'sb_publishable_68oG_98aHR0B5RPUAfLfig_TzF5ZwdK'

const args = process.argv.slice(2)
const symbol = (args.find(a => !a.startsWith('--')) || 'RELIANCE').toUpperCase()
const withChart = args.includes('--chart')
const modeIdx = args.indexOf('--mode')
const mode = (modeIdx >= 0 ? args[modeIdx + 1] : (withChart ? 'chart' : 'filings')).toLowerCase()

const H = {
  apikey: ANON,
  Authorization: `Bearer ${ANON}`,
  'Content-Type': 'application/json',
}

function crc32(buf) {
  let c, table = []
  for (let n = 0; n < 256; n++) {
    c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c >>> 0
  }
  let crc = 0xffffffff
  for (const b of buf) crc = table[(crc ^ b) & 0xff] ^ (crc >>> 8)
  return (crc ^ 0xffffffff) >>> 0
}

function chunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length)
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body))
  return Buffer.concat([len, body, crc])
}

// Crude candlestick-ish PNG so Gemini has something real to read.
function fakeChartPng(w = 640, h = 360) {
  const px = Buffer.alloc(w * h * 3, 0xff)
  const set = (x, y, r, g, b) => {
    if (x < 0 || y < 0 || x >= w || y >= h) return
    const i = (y * w + x) * 3
    px[i] = r; px[i + 1] = g; px[i + 2] = b
  }
  for (let x = 0; x < w; x++) { set(x, h - 30, 60, 60, 60); set(x, h - 29, 60, 60, 60) }
  for (let y = 0; y < h; y++) { set(50, y, 60, 60, 60); set(51, y, 60, 60, 60) }
  let price = h - 80
  for (let i = 0; i < 60; i++) {
    const x = 60 + i * 9
    const drift = -1.6 + Math.sin(i / 5) * 4
    const open = price
    const close = Math.max(40, Math.min(h - 40, price + drift))
    const top = Math.min(open, close) - 6
    const bot = Math.max(open, close) + 6
    const up = close < open
    const [r, g, b] = up ? [20, 150, 80] : [200, 50, 50]
    for (let y = top; y <= bot; y++) set(x + 3, Math.round(y), r, g, b)
    for (let y = Math.min(open, close); y <= Math.max(open, close); y++)
      for (let dx = 0; dx < 7; dx++) set(x + dx, Math.round(y), r, g, b)
    const volH = 10 + ((i * 7) % 25)
    for (let y = h - 31; y > h - 31 - volH; y--)
      for (let dx = 0; dx < 7; dx++) set(x + dx, y, 150, 160, 190)
    price = close
  }
  const raw = Buffer.alloc((w * 3 + 1) * h)
  for (let y = 0; y < h; y++) {
    raw[y * (w * 3 + 1)] = 0
    px.copy(raw, y * (w * 3 + 1) + 1, y * w * 3, (y + 1) * w * 3)
  }
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4)
  ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = 0; ihdr[11] = 0; ihdr[12] = 0
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 6 })),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

async function columnCheck() {
  const cols = ['id', 'ask_mode', 'chart_image', 'chart_image_mime']
  const out = {}
  for (const c of cols) {
    const r = await fetch(`${URL_BASE}/rest/v1/stock_ai_asks?select=${c}&limit=1`, { headers: H })
    out[c] = r.ok ? 'present' : `MISSING (${r.status}: ${(await r.text()).slice(0, 90)})`
  }
  return out
}

async function main() {
  console.log(`probe → ${symbol} mode=${mode} chart=${withChart}`)
  console.log('columns:', await columnCheck())

  const payload = {
    symbol,
    question: withChart
      ? 'Explain this chart in simple language: trend, key levels, volume and risks.'
      : 'Probe: summarise this company in two lines.',
    status: 'pending',
    ask_mode: mode,
    visitor_id: 'probe-cli',
  }
  if (withChart) {
    const b64 = fakeChartPng().toString('base64')
    payload.chart_image = b64
    payload.chart_image_mime = 'image/png'
    console.log('chart base64 length:', b64.length)
  }

  const ins = await fetch(`${URL_BASE}/rest/v1/stock_ai_asks`, {
    method: 'POST',
    headers: { ...H, Prefer: 'return=representation' },
    body: JSON.stringify(payload),
  })
  const insBody = await ins.text()
  if (!ins.ok) {
    console.error(`INSERT FAILED ${ins.status}: ${insBody.slice(0, 400)}`)
    process.exit(1)
  }
  const row = JSON.parse(insBody)[0]
  console.log('queued id:', row.id)

  const deadline = Date.now() + 180000
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, 3000))
    const g = await fetch(
      `${URL_BASE}/rest/v1/stock_ai_asks?id=eq.${row.id}&select=status,answer,verdict,error,answered_at`,
      { headers: H })
    const [cur] = await g.json()
    if (!cur) continue
    if (cur.status === 'pending') { process.stdout.write('.'); continue }
    console.log(`\nstatus: ${cur.status}`)
    if (cur.error) console.log('error:', cur.error)
    if (cur.answer) console.log('answer:', cur.answer)
    return
  }
  console.log('\nSTILL PENDING after 180s — worker is not consuming the queue.')
}

main().catch(e => { console.error(e); process.exit(1) })
