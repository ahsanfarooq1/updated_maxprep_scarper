"""
Third-stage backfill: re-stamp TotalGamesChecked in an *_all_stats_tab_*.json
file to match the (now fully corrected) gaps + Final file.

accumulate_from_stats_tab.py stamps TotalGamesChecked onto each team_total
row from the gap finder's gamesChecked count AT SCRAPE TIME (see its own
comment: "the standalone accumulator does the same — see
Accumulation_data.py"). It's a copy, not a live lookup, so once that count is
corrected (see backfill_dedupe_gaps.py and backfill_record_based_tgc.py) this
file's copy goes stale until it's independently re-stamped.

No code fix is needed in accumulate_from_stats_tab.py / Accumulation_data.py
themselves — they already just copy whatever gamesChecked the gap finder
(app.py) produces, and app.py now produces the corrected number for every
future scrape. This script only backfills the already-scraped files.

Applies the same rule as fix_total_games_checked.py / backfill_final:
    TotalGamesChecked = max(corrected_gap_count, box_count, GP)

Usage:
  python backfill_stats_tab_tgc.py \
      --stats-tab  Oregon_scraped_data/or_all_stats_tab_boys_2025_2026.json \
      --gaps       Oregon_scraped_data/or_data_gaps_boys_2025_2026.json \
      --box-scores Oregon_scraped_data/or_box_scores_boys_2025_2026.json
"""

import os
import json
import time
import shutil
import argparse

from backfill_dedupe_gaps import _build_box_count_lookup, team_url_to_path

_original_print = print
def print(*args, **kwargs):  # noqa: A001
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)


def _load_gap_count_lookup(gaps_path):
    with open(gaps_path, encoding='utf-8') as f:
        gaps = json.load(f)
    lookup = {}
    for bucket in ('teamsFullBoxScores', 'teamsPartialBoxScores', 'teamsNoBoxScores'):
        for t in gaps.get(bucket, []):
            tid = team_url_to_path(t.get('teamUrl', ''))
            tname = t.get('teamName') or ''
            gc = t.get('gamesChecked')
            if tid and gc is not None:
                lookup[(tid, tname)] = gc
    return lookup


def fix(stats_tab_path, gaps_path, box_scores_path):
    with open(stats_tab_path, encoding='utf-8') as f:
        records = json.load(f)
    if not isinstance(records, list):
        print(f'[ERROR] {stats_tab_path}: expected a flat JSON list of records.')
        return

    gap_lookup = _load_gap_count_lookup(gaps_path)
    box_lookup = _build_box_count_lookup(box_scores_path)

    updated = []
    bumped, unchanged, no_gap_match = 0, 0, 0
    examples = []
    for r in records:
        if r.get('record_type') != 'team_total':
            updated.append(r)
            continue
        key = (r.get('team_id'), r.get('team_name'))
        gp = int(r.get('GP') or 0)
        gap_count = gap_lookup.get(key)
        box_count = box_lookup.get(key, 0)
        if gap_count is None:
            no_gap_match += 1
        target_tgc = max(int(gap_count or 0), int(box_count), gp)
        current = r.get('TotalGamesChecked')

        if target_tgc != current:
            bumped += 1
            if len(examples) < 15:
                examples.append((r.get('team_name'), gp, current, target_tgc, gap_count, box_count))
            r = {**r}
            r['TotalGamesChecked'] = target_tgc
            updated.append(r)
        else:
            unchanged += 1
            updated.append(r)

    backup = f"{stats_tab_path}.bak_before_tgc_sync"
    if not os.path.exists(backup):
        shutil.copy2(stats_tab_path, backup)
    tmp = stats_tab_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=2, ensure_ascii=False)
    os.replace(tmp, stats_tab_path)

    print(f"  Stats-tab file      : {stats_tab_path}")
    print(f"  Backup              : {backup}")
    print(f"  team_total rows     : {sum(1 for r in records if r.get('record_type') == 'team_total')}")
    print(f"  TotalGamesChecked corrected : {bumped}")
    print(f"  TotalGamesChecked unchanged : {unchanged}")
    print(f"  team_totals with no matching gaps entry : {no_gap_match}")
    if examples:
        print("  Sample corrections (team, GP, old_TGC -> new_TGC, gap_count, box_count):")
        for tn, gp, old, new, gc, bc in examples:
            print(f"    {tn:35s} GP={gp:>3}  TGC: {old} -> {new}  (gap={gc}, box={bc})")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--stats-tab', required=True)
    ap.add_argument('--gaps', required=True)
    ap.add_argument('--box-scores', required=True)
    args = ap.parse_args()
    fix(args.stats_tab, args.gaps, args.box_scores)


if __name__ == '__main__':
    main()
