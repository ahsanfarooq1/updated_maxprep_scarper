"""
Second-stage backfill: correct TotalGamesChecked using MaxPreps' own season
W-L record instead of a schedule-row count.

Why this exists (see backfill_dedupe_gaps.py for the first-stage fix): a
(date, opponent_team_id) dedup catches a duplicate contest_id MaxPreps left
behind on the SAME date, but not one where a reschedule also shifted the
calendar date (e.g. a game moved from 2/10 to 2/11 leaves both dates on the
schedule feed). MaxPreps' own `overallWinLossTies` standings field is
server-computed from completed games only, so it's immune to duplicate-
contest-id artifacts of any shape — same-date or date-shifted.

This script live-fetches each team's schedule.json (via app.py's own
fetch_sched_worker, so behavior — retries, build-id refresh, rate limiting —
matches production exactly) purely to read that record field, since it
isn't stored in any already-scraped file. Then:

  1. For each team, if the record's total games (wins+losses[+ties]) is
     available and >= gamesWithStats, gamesChecked/gamesMissing are set from
     it (never allowed to undercut games we've actually confirmed have
     stats).
  2. The gaps file is rewritten (backed up first).
  3. The Final file's TotalGamesChecked is recomputed as
         max(corrected_gamesChecked, box_count, GP)
     via backfill_dedupe_gaps.backfill_final, reused as-is.

Usage:
  python backfill_record_based_tgc.py \
      --gaps          Oregon_scraped_data/or_data_gaps_boys_2025_2026.json \
      --box-scores    Oregon_scraped_data/or_box_scores_boys_2025_2026.json \
      --final         Oregon_scraped_data/Final_or_accumulated_boys_25_26.json \
      --season-suffix 25-26
"""

import os
import sys
import time
import json
import shutil
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import app as gapfinder
from backfill_dedupe_gaps import backfill_final, team_url_to_path

_original_print = print
def print(*args, **kwargs):  # noqa: A001
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)

WORKERS = gapfinder.SCHED_WORKERS


def _fetch_records(team_urls, team_names, season_suffix):
    """teamUrl -> overall_record (int total games, or None) for every team,
    fetched in parallel via app.py's own fetch_sched_worker."""
    records = {}
    failed = []
    jobs = [{"teamUrl": u, "teamName": team_names.get(u, u)} for u in team_urls]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(gapfinder.fetch_sched_worker, job, season_suffix): job for job in jobs}
        done = 0
        for fut in as_completed(futures):
            job = futures[fut]
            done += 1
            try:
                team, entries, overall_record = fut.result()
            except Exception as e:
                print(f"  [WARN] fetch crashed for {job['teamName']}: {e}")
                failed.append(job["teamUrl"])
                continue
            if entries is None:
                failed.append(job["teamUrl"])
            records[job["teamUrl"]] = overall_record
            if done % 50 == 0 or done == len(jobs):
                print(f"  Records: {done}/{len(jobs)} fetched")
    return records, failed


def backfill_gaps_with_records(gaps_path, season_suffix):
    with open(gaps_path, encoding='utf-8') as f:
        gaps = json.load(f)

    all_entries = []
    for bucket in ("teamsFullBoxScores", "teamsPartialBoxScores", "teamsNoBoxScores"):
        all_entries.extend(gaps.get(bucket, []))

    team_urls = [e["teamUrl"] for e in all_entries]
    team_names = {e["teamUrl"]: e["teamName"] for e in all_entries}
    print(f"  Fetching live records for {len(team_urls)} teams ({WORKERS} workers)...")
    records, failed = _fetch_records(team_urls, team_names, season_suffix)
    if failed:
        print(f"  [WARN] {len(failed)} teams failed to fetch — their gamesChecked is left as-is:")
        for u in failed[:20]:
            print(f"    {team_names.get(u, u)}")

    changed = 0
    for entry in all_entries:
        overall_record = records.get(entry["teamUrl"])
        entry["recordGamesPlayed"] = overall_record
        if overall_record is None:
            continue
        games_with_stats = entry.get("gamesWithStats", 0)
        if overall_record < games_with_stats:
            continue
        before = entry.get("gamesChecked")
        entry["gamesChecked"] = overall_record
        entry["gamesMissing"] = max(0, overall_record - games_with_stats)
        if entry["gamesChecked"] != before:
            changed += 1
            print(f"    [RECORD] {entry['teamName']:35s} | gamesChecked {before} -> "
                  f"{entry['gamesChecked']} (MaxPreps record: {overall_record} games played)")

    full_data, partial_data, no_data = [], [], []
    for entry in all_entries:
        gc, gm, gws = entry["gamesChecked"], entry["gamesMissing"], entry["gamesWithStats"]
        if gc == 0:            no_data.append(entry)
        elif gm == 0:           full_data.append(entry)
        elif gws > 0:           partial_data.append(entry)
        else:                   no_data.append(entry)

    total_games_checked = sum(t["gamesChecked"] for t in full_data + partial_data + no_data)
    gaps["meta"]["teamsFullBoxScores"] = len(full_data)
    gaps["meta"]["teamsPartialBoxScores"] = len(partial_data)
    gaps["meta"]["teamsNoBoxScores"] = len(no_data)
    gaps["meta"]["totalGamesChecked"] = total_games_checked
    gaps["meta"]["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    gaps["teamsFullBoxScores"] = sorted(full_data, key=lambda x: x["teamName"])
    gaps["teamsPartialBoxScores"] = sorted(partial_data, key=lambda x: x["teamName"])
    gaps["teamsNoBoxScores"] = sorted(no_data, key=lambda x: x["teamName"])

    backup = f"{gaps_path}.bak_before_record_tgc"
    if not os.path.exists(backup):
        shutil.copy2(gaps_path, backup)
    tmp = gaps_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(gaps, f, indent=2, ensure_ascii=False)
    os.replace(tmp, gaps_path)

    print(f"  Gaps file           : {gaps_path}")
    print(f"  Backup              : {backup}")
    print(f"  Teams corrected by MaxPreps record : {changed} / {len(all_entries)}")
    print(f"  Teams with no record data (fetch failed) : {len(failed)}")

    gap_count_lookup = {team_url_to_path(e["teamUrl"]): e["gamesChecked"] for e in all_entries}
    return gap_count_lookup


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--gaps', required=True)
    ap.add_argument('--box-scores', required=True)
    ap.add_argument('--final', required=True)
    ap.add_argument('--season-suffix', required=True, help='e.g. 25-26')
    args = ap.parse_args()

    print("=" * 72)
    print("STEP 1: fetch MaxPreps' own record and correct the gaps file")
    gap_count_lookup = backfill_gaps_with_records(args.gaps, args.season_suffix)

    print("=" * 72)
    print("STEP 2: recompute TotalGamesChecked on the Final file")
    backfill_final(args.final, args.box_scores, gap_count_lookup)
    print("=" * 72)


if __name__ == '__main__':
    main()
