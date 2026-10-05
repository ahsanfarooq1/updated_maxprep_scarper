"""APP/pipeline.py — single-entry 4-step MaxPreps pipeline.

Order:
  1. Gap finder              (app.py --gap-only)
        -> {state_folder}/{state}_data_gaps_{sport}_{season_fn}.json
  2. Stats-tab for EVERY team enumerated by the gap finder
        -> {state_folder}/{state}_all_stats_tab_{sport}_{season_fn}.json
  3. Box scores
        -> {state_folder}/{state}_box_scores_{sport}_{season_fn}.json
  4. Final accumulation:
       a. Per-game accumulator      → temp accumulated file
       b. Merge stats-tab on top    → Final file (stats-tab wins, GP=0 guard)
       c. Repair TotalGamesChecked  → Final file in place

Outputs:
  - {state_folder}/  has the 3 raw inputs (gaps, all_stats_tab, box_scores)
  - Final_scraped_data/Final_{state}_accumulated_{sport}_{ss}.json
        (the only file downstream consumers should read)

Usage:
  python -m APP.pipeline --state AR --sport boys --season 2025-2026
  python APP/pipeline.py  --state AR --sport boys --season 2025-2026
"""

import os
import sys
import json
import time
import shutil
import argparse
import subprocess

# This module's help text and stage banners contain →, ━ and ✓. A Windows
# console defaults to cp1252 and raises UnicodeEncodeError on them, which
# crashed `--help` outright and would abort a direct CLI run mid-stage. (The
# Streamlit launcher sets PYTHONIOENCODING for the child, so it never hit
# this — only terminal users did.)
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_ROOT)

STATE_FOLDER = {
    'AR': 'Arkansas_scraped_data',
    'LA': 'Louisiana_scraped_data',
    'NM': 'NewMaxico_scraped_data',
    'OK': 'Oklahoma_scraped_data',
    'TX': 'Texas_scraped_data',
}

FINAL_DIR = 'Final_scraped_data'


def _ts(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _run(cmd, env):
    _ts(f'$ {" ".join(cmd)}')
    rc = subprocess.run(cmd, env=env, cwd=REPO_ROOT).returncode
    if rc != 0:
        _ts(f'  ↳ exit code {rc}')
    return rc == 0


def _short_season(season):
    """'2025-2026' -> '25_26'."""
    parts = season.replace('_', '-').split('-')
    if len(parts) == 2 and all(len(p) == 4 and p.isdigit() for p in parts):
        return f'{parts[0][-2:]}_{parts[1][-2:]}'
    return season.replace('-', '_')


def run_pipeline(state, sport, season, workers, output_dir=None,
                 start_at=1, end_at=4, limit=None, level='varsity'):
    state_code = state.upper()
    state_lower = state.lower()
    season_fn = season.replace('-', '_')
    ss = _short_season(season)

    if output_dir:
        state_folder = output_dir
        final_folder = output_dir
    else:
        state_folder = STATE_FOLDER.get(state_code, f'{state_code}_scraped_data')
        final_folder = FINAL_DIR
    os.makedirs(state_folder, exist_ok=True)
    os.makedirs(final_folder, exist_ok=True)

    # Level suffix ('' for varsity, '_jv', '_freshman') keeps every varsity
    # filename byte-identical to before while giving JV/freshman their own set,
    # so the three levels can coexist without overwriting each other.
    from scrape_box_scores import normalise_level, level_file_suffix
    level = normalise_level(level)
    lv = level_file_suffix(level)

    gaps_path  = os.path.join(state_folder, f'{state_lower}_data_gaps_{sport}{lv}_{season_fn}.json')
    stab_path  = os.path.join(state_folder, f'{state_lower}_all_stats_tab_{sport}{lv}_{season_fn}.json')
    box_path   = os.path.join(state_folder, f'{state_lower}_box_scores_{sport}{lv}_{season_fn}.json')
    acc_path   = os.path.join(state_folder, f'{state_lower}_accumulated_stats_{sport}{lv}_{season_fn}.json')
    final_path = os.path.join(final_folder, f'Final_{state_lower}_accumulated_{sport}{lv}_{ss}.json')

    env = os.environ.copy()
    env['DATA_DIR'] = state_folder
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8']       = '1'
    env['PYTHONUNBUFFERED'] = '1'

    py = [sys.executable, '-u', '-X', 'utf8']

    print('=' * 80)
    _ts(f'APP/pipeline  state={state_code}  sport={sport}  season={season}  level={level}')
    _ts(f'  state folder     : {state_folder}/')
    _ts(f'  final folder     : {final_folder}/')
    _ts(f'  stages {start_at}-{end_at}')
    print('=' * 80)

    # ── STAGE 1: GAP FINDER ──────────────────────────────────────────────
    if start_at <= 1 <= end_at:
        print()
        _ts('━ STAGE 1/4: Gap finder ━')
        ok = _run(py + ['app.py',
                         '--state',  state_code,
                         '--sport',  sport,
                         '--season', season,
                         '--level',  level,
                         '--gap-only'],
                  env)
        if not ok:
            _ts('STAGE 1 FAILED — stopping.')
            return False

    # ── STAGE 2: STATS-TAB FOR EVERY TEAM ────────────────────────────────
    # Uses accumulate_from_stats_tab.py which already accepts a gaps file as
    # input (it walks teamsFullBoxScores + teamsPartial + teamsNo). This gives
    # us EVERY team the gap finder discovered, no accumulated file needed.
    if start_at <= 2 <= end_at:
        print()
        _ts('━ STAGE 2/4: Stats-tab scraper (every team) ━')
        if not os.path.exists(gaps_path):
            _ts(f'  SKIP — gaps file missing: {gaps_path}')
        else:
            ok = _run(py + ['accumulate_from_stats_tab.py',
                             '--input',   gaps_path,
                             '--season',  season,
                             '--level',   level,
                             '--workers', str(workers),
                             '--output',  stab_path],
                      env)
            if not ok:
                _ts('STAGE 2 FAILED — stopping.')
                return False

    # ── STAGE 3: BOX SCORES ──────────────────────────────────────────────
    if start_at <= 3 <= end_at:
        print()
        _ts('━ STAGE 3/4: Box-score scraper ━')
        if not os.path.exists(gaps_path):
            _ts(f'  SKIP — gaps file missing: {gaps_path}')
        else:
            box_cmd = ['scrape_box_scores.py',
                       '--state',  state_code,
                       '--sport',  sport,
                       '--season', season,
                       '--level',  level,
                       '--input',  gaps_path,
                       '--output', box_path,
                       '--workers', str(workers),
                       '--no-accumulate']
            if limit:
                box_cmd += ['--limit', str(limit)]
            ok = _run(py + box_cmd, env)
            if not ok:
                _ts('STAGE 3 FAILED — stopping.')
                return False

    # ── STAGE 4: FINAL ACCUMULATION ──────────────────────────────────────
    # 4a) Per-game accumulator (Accumulation_data.py). It reads the box-scores
    #     file and also picks up the gaps file alongside it for TGC.
    # 4b) Merge stats-tab over the per-game output (stats-tab wins, GP=0 guard).
    # 4c) Rewrite TotalGamesChecked using max(current, distinct box_count, GP).
    if start_at <= 4 <= end_at:
        print()
        _ts('━ STAGE 4/4: Final accumulation (acc + merge + TGC fix) ━')
        if not os.path.exists(box_path):
            _ts(f'  SKIP — box scores file missing: {box_path}')
        else:
            _ts('  4a) per-game accumulator')
            ok = _run(py + ['-c',
                             ('import sys, os; '
                              f'sys.path.insert(0, {REPO_ROOT!r}); '
                              'from Accumulation_data import process_stats; '
                              f'process_stats(input_file={box_path!r}, output_file={acc_path!r})')],
                      env)
            if not ok:
                _ts('STAGE 4a (accumulator) FAILED — stopping.')
                return False

            _ts('  4b) merge stats-tab into accumulated')
            if not os.path.exists(stab_path):
                # No stats-tab file (e.g. stage 2 was skipped/failed) — just
                # copy accumulator output straight into the Final file so the
                # downstream contract still holds (Final always exists).
                shutil.copyfile(acc_path, final_path)
                _ts(f'    (no stats-tab file) Copied accumulated → {final_path}')
            else:
                ok = _run(py + ['merge_all_stats_tab.py',
                                 '--accumulated', acc_path,
                                 '--stats-tab',   stab_path,
                                 '--output',      final_path],
                          env)
                if not ok:
                    _ts('STAGE 4b (merge) FAILED — stopping.')
                    return False

            _ts('  4c) repair TotalGamesChecked')
            ok = _run(py + ['fix_total_games_checked.py',
                             '--input',      final_path,
                             '--box-scores', box_path,
                             '--output',     final_path],
                      env)
            if not ok:
                _ts('STAGE 4c (TGC fix) FAILED — stopping.')
                return False

    print()
    print('=' * 80)
    _ts('PIPELINE COMPLETE — outputs:')
    for label, p in [
        ('gaps',           gaps_path),
        ('all_stats_tab',  stab_path),
        ('box_scores',     box_path),
        ('accumulated',    acc_path),
        ('FINAL',          final_path),
    ]:
        flag = '✓' if os.path.exists(p) else '·'
        print(f'  {flag} {label:<14} {p}')
    print('=' * 80)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state',   required=True,
                    help='State code (TX, AR, LA, NM, OK, …).')
    ap.add_argument('--sport',   required=True, choices=['boys', 'girls'])
    ap.add_argument('--season',  required=True,
                    help='Season (e.g. 2025-2026 or 2024-2025).')
    ap.add_argument('--workers', type=int, default=15,
                    help='Parallel worker count for stages 2 and 3.')
    ap.add_argument('--start-at', type=int, default=1, choices=range(1, 5),
                    help='Skip to a specific stage (1-4). Default 1.')
    ap.add_argument('--end-at',   type=int, default=4, choices=range(1, 5),
                    help='Stop after a specific stage (1-4). Default 4.')
    ap.add_argument('--level', default='varsity',
                    choices=['varsity', 'jv', 'freshman'],
                    help='Team level (default: varsity). JV/freshman write to '
                         'their own *_jv / *_freshman output files.')
    ap.add_argument('--limit', type=int, default=None,
                    help='Box-score stage: scrape only the first N unprocessed '
                         'teams. Use for a quick end-to-end smoke test before '
                         'committing to a full state run.')
    ap.add_argument('--output-dir', default=None,
                    help='Override the per-state folder. If set, ALL stage '
                         'outputs go here (handy for Streamlit / cloud runs).')
    args = ap.parse_args()

    if args.start_at > args.end_at:
        print('--start-at must be ≤ --end-at')
        sys.exit(2)

    ok = run_pipeline(args.state, args.sport, args.season, args.workers,
                      output_dir=args.output_dir,
                      start_at=args.start_at, end_at=args.end_at,
                      limit=args.limit, level=args.level)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
