"""Batch driver: scrape stats-tab + merge + TGC fix for every (state, sport,
season) combination, writing the final files to Final_scraped_data/.

For each dataset:
  1. scrape_all_stats_tab.py  (enumerates every canonical team in the
     accumulated file, scrapes the print-stats endpoint, writes
     {state}_all_stats_tab_{sport}_{season}.json into the state folder)
  2. merge_all_stats_tab.py   (merges stats-tab into accumulated-updated
     with the GP=0 guard, writes Final_scraped_data/Final_{state}_
     accumulated_{sport}_{ss}.json)
  3. fix_total_games_checked.py  (rewrites TotalGamesChecked using
     max(current, box_count, GP) so GP <= TGC always holds)

Skips a stage if its input is missing and prints a clear warning."""

import os
import sys
import time
import subprocess

STATE_FOLDER = {
    'ar': 'Arkansas_scraped_data',
    'la': 'Louisiana_scraped_data',
    'nm': 'NewMaxico_scraped_data',
    'ok': 'Oklahoma_scraped_data',
}

# (sport, full-season, short-season)
DATASETS = [
    ('boys',  '2024-2025', '24_25'),
    ('boys',  '2025-2026', '25_26'),
    ('girls', '2024-2025', '24_25'),
    ('girls', '2025-2026', '25_26'),
]

FINAL_DIR = 'Final_scraped_data'
os.makedirs(FINAL_DIR, exist_ok=True)

PY = [sys.executable, '-u', '-X', 'utf8']
ENV = {**os.environ, 'PYTHONIOENCODING': 'utf-8',
       'PYTHONUTF8': '1', 'PYTHONUNBUFFERED': '1'}


def ts():
    return time.strftime('[%Y-%m-%d %H:%M:%S]')


def run(cmd):
    print(f'{ts()} $ {" ".join(cmd)}', flush=True)
    rc = subprocess.run(cmd, env=ENV).returncode
    if rc != 0:
        print(f'{ts()}   exit code {rc}', flush=True)
    return rc == 0


def process_one(state, sport, season, short_season):
    sf = STATE_FOLDER[state]
    season_fn = season.replace('-', '_')

    acc          = f'{sf}/{state}_accumulated_stats_{sport}_{season_fn}.json'
    acc_updated  = f'{sf}/{state}_accumulated_stats_{sport}_{season_fn}_updated.json'
    box_scores   = f'{sf}/{state}_box_scores_{sport}_{season_fn}.json'
    all_stab     = f'{sf}/{state}_all_stats_tab_{sport}_{season_fn}.json'
    final_out    = f'{FINAL_DIR}/Final_{state}_accumulated_{sport}_{short_season}.json'

    label = f'{state.upper()} {sport} {short_season}'
    print()
    print('=' * 80)
    print(f'{ts()} ▶ {label}')
    print('=' * 80)

    # Sanity-check inputs upfront
    missing = []
    if not os.path.exists(acc):
        missing.append(acc)
    if not os.path.exists(acc_updated):
        missing.append(acc_updated)
    if not os.path.exists(box_scores):
        missing.append(box_scores)
    if missing:
        print(f'{ts()}  ✗ missing inputs — SKIP: ' + ', '.join(missing))
        return False

    # 1) Scrape stats-tab for every team
    print(f'{ts()}  [1/3] scrape_all_stats_tab.py')
    if not run(PY + ['scrape_all_stats_tab.py',
                     '--accumulated', acc,
                     '--season',      season,
                     '--workers',     '15',
                     '--output',      all_stab]):
        return False

    # 2) Merge stats-tab into accumulated_updated (stats-tab wins, GP=0 guard)
    print(f'{ts()}  [2/3] merge_all_stats_tab.py')
    if not run(PY + ['merge_all_stats_tab.py',
                     '--accumulated', acc_updated,
                     '--stats-tab',   all_stab,
                     '--output',      final_out]):
        return False

    # 3) Repair TotalGamesChecked using box-score counts + GP
    print(f'{ts()}  [3/3] fix_total_games_checked.py')
    if not run(PY + ['fix_total_games_checked.py',
                     '--input',      final_out,
                     '--box-scores', box_scores,
                     '--output',     final_out]):
        return False

    print(f'{ts()}  ✓ {label} → {final_out}')
    return True


def main():
    overall_start = time.time()
    results = []
    states = sys.argv[1:] if len(sys.argv) > 1 else ['ar', 'la', 'nm', 'ok']
    for st in states:
        for sport, season, ss in DATASETS:
            t0 = time.time()
            ok = process_one(st, sport, season, ss)
            dt = time.time() - t0
            results.append((st.upper(), sport, ss, ok, dt))

    print()
    print('=' * 80)
    print(f'{ts()} OVERALL SUMMARY  ({time.time()-overall_start:.1f}s total)')
    print('=' * 80)
    print(f'{"state":>5s} {"sport":>6s} {"season":>7s}   {"result":<8s}  {"elapsed":>9s}')
    for st, sport, ss, ok, dt in results:
        flag = '✓ ok' if ok else '✗ FAIL'
        print(f'{st:>5s} {sport:>6s} {ss:>7s}   {flag:<8s}  {dt:>7.1f}s')
    print('=' * 80)


if __name__ == '__main__':
    main()
