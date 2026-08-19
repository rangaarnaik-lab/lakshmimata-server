"""Generate a one-time SQL backfill for stocks.sector / stocks.industry.

The BOM bug in _load_sector_industry_lookup() (fixed in shared.py) meant the
static lookup loaded 0 of 2,355 symbols, so ~2,388 stocks sat at sector='Other'
with industry=NULL. The deployed scan repairs this on its own once it restarts,
but this script produces SQL to fix the table immediately.

Values are computed with the same precedence as shared.get_sector() /
get_industry(), and the constants are parsed straight out of shared.py rather
than copied, so this can't silently drift from the real logic. shared.py isn't
importable here (it pulls in aiohttp/boto3 at module level), hence the AST read.

Usage:  python scripts/gen_sector_industry_backfill.py
Output: backfill_sector_industry.sql  (paste into the Supabase SQL editor)
"""
import ast
import csv
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED = os.path.join(ROOT, 'shared.py')
LOOKUP_CSV = os.path.join(ROOT, 'data', 'sector_industry_lookup.csv')
OUT_SQL = os.path.join(ROOT, 'backfill_sector_industry.sql')

WANTED = [
    'SECTOR_MAP', 'QSR_INDUSTRY_OVERRIDES', 'QSR_SECTOR', '_SECTOR_ALIASES',
    'PHARMA_FORMULATIONS', 'PHARMA_BULK_DRUGS',
    'PHARMA_FORMULATION_SYMS', 'PHARMA_BULK_API_SYMS',
]


def read_constants() -> dict:
    """Pull the literal constants get_sector()/get_industry() depend on."""
    with open(SHARED, encoding='utf-8') as f:
        tree = ast.parse(f.read(), filename=SHARED)
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in WANTED:
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    missing = [n for n in WANTED if n not in found]
    if missing:
        sys.exit(f'Could not parse from shared.py: {", ".join(missing)} — '
                 f'did these stop being plain literal assignments?')
    return found


def load_lookup() -> dict:
    lookup = {}
    with open(LOOKUP_CSV, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            row = {(k or '').strip().lstrip('\ufeff'): v for k, v in row.items()}
            sym = (row.get('symbol') or '').strip().upper()
            if sym:
                lookup[sym] = {
                    'industry': (row.get('industry') or '').strip() or None,
                    'sector': (row.get('sector') or '').strip() or None,
                }
    if not lookup:
        sys.exit(f'Lookup CSV loaded 0 symbols from {LOOKUP_CSV}')
    return lookup


def sql_str(v) -> str:
    if v is None:
        return 'NULL'
    return "'" + str(v).replace("'", "''") + "'"


def main():
    C = read_constants()
    lookup = load_lookup()

    sym_to_curated = {}
    for sector, syms in C['SECTOR_MAP'].items():
        for s in syms:
            sym_to_curated.setdefault(s.strip().upper(), sector)

    def canon_sector(name):
        if not name:
            return name
        return C['_SECTOR_ALIASES'].get(name.strip().lower(), name.strip())

    def sector_for(key):
        if key in C['QSR_INDUSTRY_OVERRIDES']:
            return C['QSR_SECTOR']
        if key in sym_to_curated:
            return sym_to_curated[key]
        return canon_sector(lookup.get(key, {}).get('sector'))

    def industry_for(key):
        """Returns (value, force). force=True when the value doesn't depend on
        the live fundamentals cache, so it's safe to overwrite what's in the
        table; otherwise we only fill NULLs and leave a live-scraped value be,
        matching get_industry()'s live-first precedence."""
        if key in C['QSR_INDUSTRY_OVERRIDES']:
            return C['QSR_INDUSTRY_OVERRIDES'][key], True
        if key in C['PHARMA_FORMULATION_SYMS']:
            return C['PHARMA_FORMULATIONS'], True
        if key in C['PHARMA_BULK_API_SYMS']:
            return C['PHARMA_BULK_DRUGS'], True
        raw = lookup.get(key, {}).get('industry')
        if raw and re.search(r'Bulk Drugs\s*&\s*Form', raw, re.I):
            return C['PHARMA_BULK_DRUGS'], True
        return raw, False

    symbols = sorted(set(lookup) | set(sym_to_curated) | set(C['QSR_INDUSTRY_OVERRIDES']))

    rows = []
    for key in symbols:
        sector = sector_for(key)
        industry, force = industry_for(key)
        if not sector and not industry:
            continue
        rows.append((key, sector, industry, force))

    n_sec = sum(1 for r in rows if r[1])
    n_ind = sum(1 for r in rows if r[2])
    sugar = sum(1 for r in rows if r[2] == 'Sugar')

    with open(OUT_SQL, 'w', encoding='utf-8', newline='\n') as f:
        w = f.write
        w('-- One-time backfill of stocks.sector / stocks.industry.\n')
        w('--\n')
        w('-- The static sector/industry lookup silently loaded 0 of 2,355 symbols\n')
        w('-- (UTF-8 BOM made csv.DictReader name the first column \\ufeffsymbol), so\n')
        w("-- every stock outside the hand-curated SECTOR_MAP fell back to sector\n")
        w("-- 'Other' with industry NULL and vanished from the sector/industry\n")
        w('-- rankings. shared.py is fixed; this repairs the existing rows now\n')
        w('-- instead of waiting for a deploy + scan cycle.\n')
        w('--\n')
        w(f'-- Generated by scripts/gen_sector_industry_backfill.py\n')
        w(f'-- {len(rows)} symbols: {n_sec} with a sector, {n_ind} with an industry '
          f'({sugar} of them Sugar).\n')
        w('--\n')
        w('-- Sector is authoritative (curated SECTOR_MAP first, then the lookup CSV)\n')
        w('-- so it overwrites. Industry only overwrites where the value does not\n')
        w('-- depend on the live fundamentals scrape; elsewhere it fills NULLs and\n')
        w('-- leaves any live-scraped value alone. Symbols absent from both sources\n')
        w('-- are left untouched.\n\n')

        w('BEGIN;\n\n')
        w('CREATE TEMP TABLE _si (\n')
        w('  sym       text PRIMARY KEY,\n')
        w('  sector    text,\n')
        w('  industry  text,\n')
        w('  force_ind boolean NOT NULL\n')
        w(') ON COMMIT DROP;\n\n')

        w('INSERT INTO _si (sym, sector, industry, force_ind) VALUES\n')
        for i, (sym, sector, industry, force) in enumerate(rows):
            end = ',\n' if i < len(rows) - 1 else ';\n\n'
            w(f'  ({sql_str(sym)},{sql_str(sector)},{sql_str(industry)},'
              f'{"true" if force else "false"}){end}')

        w('UPDATE public.stocks s\n')
        w('SET sector   = COALESCE(v.sector, s.sector),\n')
        w('    industry = CASE WHEN v.force_ind THEN v.industry\n')
        w('                    ELSE COALESCE(s.industry, v.industry) END\n')
        w('FROM _si v\n')
        w('WHERE s.sym = v.sym;\n\n')
        w('COMMIT;\n\n')

        w('-- Verify: sector "Other" should drop from ~2,388 to a few hundred,\n')
        w('-- and Sugar should now exist as an industry.\n')
        w('SELECT count(*) FILTER (WHERE sector = \'Other\')  AS sector_other,\n')
        w('       count(*) FILTER (WHERE industry IS NULL)  AS industry_null,\n')
        w("       count(*) FILTER (WHERE industry = 'Sugar') AS sugar,\n")
        w('       count(*)                                   AS total\n')
        w('FROM public.stocks;\n')

    print(f'wrote {OUT_SQL}')
    print(f'  {len(rows)} symbols — {n_sec} sectors, {n_ind} industries, {sugar} Sugar')
    print(f'  {os.path.getsize(OUT_SQL) / 1024:.0f} KB')


if __name__ == '__main__':
    main()
