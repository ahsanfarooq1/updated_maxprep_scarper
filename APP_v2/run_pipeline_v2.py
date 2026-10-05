"""
run_pipeline_v2.py — orchestrator for the updated 3-stage pipeline.

Stage A (combined_scraper.py) and Stage B (stats-tab) now run
CONCURRENTLY as two subprocesses, instead of A -> B -> C sequentially:

  OLD (APP/pipeline.py, 4 sequential stages):
    1. Gap finder          (app.py)
    2. Stats-tab           (accumulate_from_stats_tab.py)  <- waits on 1
    3. Box scores          (scrape_box_scores.py)          <- re-fetches
                                                                everything
                                                                stage 1
                                                                already
                                                                fetched
    4. Accumulation + merge + TGC fix

  NEW (this file, 3 stages, A+B concurrent):
    A. Combined gap-find + box-score fetch (combined_scraper.py)  -\
    B. Stats-tab (accumulate_from_stats_tab.py, same script,        > parallel
       unchanged - just given a flat team list directly instead   -/
       of waiting on stage A's gaps output)
    C. Accumulation + merge + TGC fix - UNCHANGED, same 3 scripts
       APP/pipeline.py already calls (Accumulation_data.py,
       merge_all_stats_tab.py, fix_total_games_checked.py)

Stage B doesn't need stage A's output to start - it only ever needed the
team LIST, which comes from the master {sport}_basketball_all_states.json
file either way. Running them in parallel is a second, smaller time saving
on top of combined_scraper.py's main one (one fetch per game instead of
two).

Usage:
  python run_pipeline_v2.py --state OR --sport boys --season 2026-2027
"""

import os
import sys
import json
import time
import shutil
import argparse
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, REPO_ROOT)

STATE_FOLDER = {
    'AR': 'Arkansas_scraped_data',
    'LA': 'Louisiana_scraped_data',
    'NM': 'NewMaxico_scraped_data',
    'OK': 'Oklahoma_scraped_data',
    'TX': 'Texas_scraped_data',
    'CO': 'Colorado_Scraped_data',
    'IN': 'Indiana_scraped_data',
    'OH': 'Ohio_scraped_data',
    'WA': 'Washington_scraped_data',
    'MI': 'Michigan_scraped_data',
}
FINAL_DIR = 'Final_scraped_data'


def _ts(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _run(cmd, env, label):
    _ts(f'$ [{label}] {" ".join(cmd)}')
    rc = subprocess.run(cmd, env=env, cwd=REPO_ROOT).returncode
    if rc != 0:
        _ts(f'  [{label}] exit code {rc}')
    return rc == 0


def _short_season(season):
    parts = season.replace('_', '-').split('-')
    if len(parts) == 2 and all(len(p) == 4 and p.isdigit() for p in parts):
        return f'{parts[0][-2:]}_{parts[1][-2:]}'
    return season.replace('-', '_')


def _build_flat_team_list(state_code, sport, season, tmp_path, limit=None):
    """Pre-flatten the master team list for one state/sport into the
    {teamUrl, teamName} shape accumulate_from_stats_tab.py already accepts
    (format 2/3) - so stage B doesn't need to wait for stage A's gaps file
    just to get a team list it could have had from the start."""
    input_file = os.path.join(REPO_ROOT, f"{sport}_basketball_all_states_{season}.json")
    if not os.path.exists(input_file):
        input_file = os.path.join(REPO_ROOT, f"{sport}_basketball_all_states.json")
    with open(input_file, encoding="utf-8") as f:
        data = json.load(f)
    if state_code not in data.get("byState", {}):
        raise SystemExit(f"State {state_code} not found in master team list {input_file}")

    seen, teams = set(), []
    for r, d in data["byState"][state_code]["regions"].items():
        for t in d["teams"]:
            url = t.get("teamUrl", "")
            if url and url not in seen:
                seen.add(url)
                teams.append({"teamUrl": url, "teamName": t.get("teamName", "")})

    if limit and limit > 0:
        teams = teams[:limit]

    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(teams, f)
    return len(teams)


def run_pipeline(state, sport, season, workers=15, output_dir=None, level='varsity', limit=None):
    state_code = state.upper()
    state_lower = state_code.lower()
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

    from scrape_box_scores import normalise_level, level_file_suffix
    level = normalise_level(level)
    lv = level_file_suffix(level)

    gaps_path  = os.path.join(state_folder, f'{state_lower}_data_gaps_{sport}{lv}_{season_fn}.json')
    stab_path  = os.path.join(state_folder, f'{state_lower}_all_stats_tab_{sport}{lv}_{season_fn}.json')
    box_path   = os.path.join(state_folder, f'{state_lower}_box_scores_{sport}{lv}_{season_fn}.json')
    acc_path   = os.path.join(state_folder, f'{state_lower}_accumulated_stats_{sport}{lv}_{season_fn}.json')
    final_path = os.path.join(final_folder, f'Final_{state_lower}_accumulated_{sport}{lv}_{ss}.json')
    flat_teams_path = os.path.join(state_folder, f'.tmp_{state_lower}_{sport}_team_list.json')

    env = os.environ.copy()
    env['DATA_DIR'] = state_folder
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    env['PYTHONUNBUFFERED'] = '1'
    py = [sys.executable, '-u']

    print('=' * 80)
    _ts(f'run_pipeline_v2  state={state_code}  sport={sport}  season={season}  level={level}')
    _ts(f'  state folder : {state_folder}/')
    _ts(f'  final folder : {final_folder}/')
    print('=' * 80)

    # ── STAGE A + B: combined scraper and stats-tab, CONCURRENTLY ────────
    n_teams = _build_flat_team_list(state_code, sport, season, flat_teams_path, limit=limit)
    _ts(f'  Flattened team list: {n_teams} teams -> {flat_teams_path}')

    print()
    # "STAGE 1/4" / "STAGE 2/4" banners below are for streamlit_app.py's
    # existing log parser only (parse_log keys its phase tracking off these
    # exact substrings) - printed up front so BOTH concurrent subprocesses'
    # interleaved output gets attributed to a sensible phase, without
    # needing to touch that parser at all. combined_scraper.py's own Phase
    # 1/Phase 2 prints (schedules, then Full:/Part:) are what used to be
    # reported as stage 1 AND stage 3 combined - correctly still phase 1
    # from the UI's point of view, since that's the same underlying work.
    _ts('STAGE 1/4: combined gap+box-score fetch (replaces old stages 1+3)')
    _ts('STAGE 2/4: stats-tab (running concurrently with stage 1, not after it)')
    combined_cmd = py + [os.path.join(SCRIPT_DIR, 'combined_scraper.py'),
                         '--state', state_code, '--sport', sport, '--season', season,
                         '--level', level, '--workers', str(workers),
                         '--gaps-output', gaps_path, '--box-output', box_path]
    if limit:
        combined_cmd += ['--limit', str(limit)]
    proc_a = subprocess.Popen(combined_cmd, cwd=REPO_ROOT, env=env)
    proc_b = subprocess.Popen(
        py + ['accumulate_from_stats_tab.py',
              '--input', flat_teams_path, '--season', season, '--level', level,
              '--workers', str(workers), '--output', stab_path],
        cwd=REPO_ROOT, env=env)

    rc_a = proc_a.wait()
    rc_b = proc_b.wait()
    try:
        os.remove(flat_teams_path)
    except OSError:
        pass

    if rc_a != 0:
        _ts(f'STAGE A FAILED (exit {rc_a}) - stopping.')
        return False
    if rc_b != 0:
        _ts(f'  [WARN] STAGE B (stats-tab) exited {rc_b} - continuing; '
            f'stage C will fall back to box-score-only data for every team.')

    # ── STAGE C: accumulation + merge + TGC fix - UNCHANGED ──────────────
    print()
    _ts('STAGE 4/4: accumulation + merge + TGC fix')
    if not os.path.exists(box_path):
        _ts(f'  SKIP - box scores file missing: {box_path}')
        return False

    _ts('  4a) per-game accumulator')
    ok = _run(py + ['-c',
                     ('import sys; '
                      f'sys.path.insert(0, {REPO_ROOT!r}); '
                      'from Accumulation_data import process_stats; '
                      f'process_stats(input_file={box_path!r}, output_file={acc_path!r})')],
              env, 'C-accumulate')
    if not ok:
        _ts('STAGE C (accumulator) FAILED - stopping.')
        return False

    if not os.path.exists(stab_path):
        shutil.copyfile(acc_path, final_path)
        _ts(f'  (no stats-tab file) copied accumulated -> {final_path}')
    else:
        ok = _run(py + ['merge_all_stats_tab.py',
                         '--accumulated', acc_path, '--stats-tab', stab_path,
                         '--output', final_path],
                  env, 'C-merge')
        if not ok:
            _ts('STAGE C (merge) FAILED - stopping.')
            return False

    ok = _run(py + ['fix_total_games_checked.py',
                     '--input', final_path, '--box-scores', box_path,
                     '--output', final_path],
              env, 'C-fix-tgc')
    if not ok:
        _ts('STAGE C (TGC fix) FAILED - stopping.')
        return False

    print()
    print('=' * 80)
    _ts('PIPELINE COMPLETE')
    for label, p in [('gaps', gaps_path), ('all_stats_tab', stab_path),
                     ('box_scores', box_path), ('accumulated', acc_path),
                     ('FINAL', final_path)]:
        flag = 'OK' if os.path.exists(p) else '--'
        print(f'  [{flag}] {label:<14} {p}')
    print('=' * 80)
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--state', required=True)
    ap.add_argument('--sport', required=True, choices=['boys', 'girls'])
    ap.add_argument('--season', required=True)
    ap.add_argument('--workers', type=int, default=15)
    ap.add_argument('--level', default='varsity', choices=['varsity', 'jv', 'freshman'])
    ap.add_argument('--output-dir', default=None)
    ap.add_argument('--limit', type=int, default=None,
                    help='Process only the first N teams - for a quick smoke test.')
    args = ap.parse_args()
    ok = run_pipeline(args.state, args.sport, args.season, workers=args.workers,
                      output_dir=args.output_dir, level=args.level, limit=args.limit)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
