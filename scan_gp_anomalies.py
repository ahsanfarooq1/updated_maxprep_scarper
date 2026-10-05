"""One-off audit: how many teams across ALL accumulated files have GP > TotalGamesChecked?"""
import json, os, glob
from collections import Counter

folders = ['Arkansas_scraped_data', 'Louisiana_scraped_data',
           'NewMaxico_scraped_data', 'Oklahoma_scraped_data',
           'Texas_scraped_data']

files = []
for folder in folders:
    if os.path.isdir(folder):
        files.extend(sorted(glob.glob(os.path.join(folder, '*_accumulated_stats_*.json'))))

print(f'Scanning {len(files)} accumulated files...\n')
header = f'{"File":68s}  total  GP>TGC  GP==TGC  GP<TGC  GP=0/no-TGC'
print(header)
print('-' * len(header))

grand_anom = []
totals = dict(t=0, gt=0, eq=0, lt=0, zero_or_missing=0)
for fp in files:
    with open(fp, encoding='utf-8') as f:
        recs = json.load(f)
    if not isinstance(recs, list):
        continue
    tt = [r for r in recs if r.get('record_type') == 'team_total']
    cnt = dict(t=0, gt=0, eq=0, lt=0, zero_or_missing=0)
    for r in tt:
        gp = r.get('GP') or 0
        tgc = r.get('TotalGamesChecked')
        cnt['t'] += 1
        if tgc is None or gp == 0:
            cnt['zero_or_missing'] += 1
            continue
        if gp > tgc:
            cnt['gt'] += 1
            grand_anom.append({
                'file':    fp,
                'team':    r.get('team_name'),
                'team_id': r.get('team_id'),
                'GP':      gp,
                'TGC':     tgc,
                'diff':    gp - tgc,
            })
        elif gp == tgc:
            cnt['eq'] += 1
        else:
            cnt['lt'] += 1
    for k in totals:
        totals[k] += cnt[k]
    short = os.path.basename(fp)
    print(f'{short:68s}  {cnt["t"]:5d}  {cnt["gt"]:6d}  {cnt["eq"]:7d}  {cnt["lt"]:6d}  {cnt["zero_or_missing"]:11d}')

print('-' * len(header))
print(f'{"TOTAL":68s}  {totals["t"]:5d}  {totals["gt"]:6d}  {totals["eq"]:7d}  {totals["lt"]:6d}  {totals["zero_or_missing"]:11d}')

print()
print(f'Teams with GP > TotalGamesChecked: {len(grand_anom)}')
print(f'Sum of excess games (GP - TGC):    {sum(x["diff"] for x in grand_anom)}')
print()
print('Distribution of the GP - TGC gap:')
for diff, c in sorted(Counter(x['diff'] for x in grand_anom).items()):
    print(f'  +{diff}:  {c} teams')

print()
print('Top 20 worst offenders (largest GP - TGC):')
for a in sorted(grand_anom, key=lambda x: -x['diff'])[:20]:
    print(f'  +{a["diff"]:>2}  GP={a["GP"]:>2} TGC={a["TGC"]:>2}  {a["team"]:40s}  ({os.path.basename(a["file"])})')

with open('gp_exceeds_gameschecked_anomalies.json', 'w', encoding='utf-8') as f:
    json.dump({
        'description':       'Teams where team_total.GP > TotalGamesChecked across all accumulated files (originals + _updated)',
        'totalAnomalies':    len(grand_anom),
        'sumOfExcessGames':  sum(x['diff'] for x in grand_anom),
        'anomalies':         grand_anom,
    }, f, indent=2, ensure_ascii=False)
print()
print('Full list written to: gp_exceeds_gameschecked_anomalies.json')
