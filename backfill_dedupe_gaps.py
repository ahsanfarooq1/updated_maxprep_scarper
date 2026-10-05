"""
One-off backfill: apply app.py's (date, opponent_team_id) + stats-fingerprint
dedupe rule (see _dedupe_games_by_date_opponent) to an ALREADY-SCRAPED gaps
file, then recompute TotalGamesChecked on the paired Final/accumulated file.

MaxPreps' schedule feed occasionally carries two contest_ids for one real
matchup (a stale entry left behind after a game's start time was edited).
Both were previously counted as separate "games checked", inflating
gamesChecked / TotalGamesChecked. This script:

  1. Re-groups every team's classified games (fullDataGames / teamOnlyData /
     opponentOnlyData / noDataGames) by (date, opponent_team_id).
  2. For a group with more than one entry:
       - only one entry has real stats  -> the rest are stale no_data
         placeholders of that same game. Drop them.
       - two+ entries have real stats   -> compare their actual box-score
         content (via the box-scores file, matched by team_id+contest_id):
           identical  -> same game reported twice, keep one
           different  -> genuine doubleheader, keep BOTH
       - all entries are no_data        -> nothing to compare; collapse to
         one and flag it for manual review.
  3. Rewrites the gaps file (backed up to <path>.bak.<timestamp> first).
  4. Recomputes TotalGamesChecked on the Final file as
         max(corrected_gamesChecked, box_count, GP)
     — the OLD TotalGamesChecked is intentionally NOT part of this max
     (unlike fix_total_games_checked.py's ongoing rule), because it's
     exactly the inflated number this script corrects.

Nothing is re-scraped; this only reconciles already-collected data.

Usage:
  python backfill_dedupe_gaps.py \
      --gaps       Oregon_scraped_data/or_data_gaps_boys_2025_2026.json \
      --box-scores Oregon_scraped_data/or_box_scores_boys_2025_2026.json \
      --final      Oregon_scraped_data/Final_or_accumulated_boys_25_26.json
"""

import os
import re
import json
import time
import hashlib
import argparse
import shutil
from collections import defaultdict

_original_print = print
def print(*args, **kwargs):  # noqa: A001
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)

PRIORITY = {"full": 0, "team_only": 1, "opp_only": 1, "no_data": 2}
BUCKET_KEY = {
    "full":      "fullDataGames",
    "team_only": "teamOnlyDataGames",
    "opp_only":  "opponentOnlyDataGames",
    "no_data":   "noDataGames",
}
DEFAULT_NOTE = {
    "full":      "Both teams entered stats.",
    "team_only": "Only THIS team entered stats; opponent did not.",
    "opp_only":  "Only the OPPONENT entered stats; this team did not.",
    "no_data":   "NEITHER team entered any stats.",
}


def team_url_to_path(team_url):
    return re.sub(r"https://www\.maxpreps\.com/", "", team_url or "").rstrip("/")


def _contest_id_from_url(url):
    m = re.search(r"[?&]c=([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else None


def _players_key(players):
    return sorted(
        (p.get("player_name", ""), p.get("minutes_played"), p.get("points"),
         p.get("fg_made"), p.get("fg_attempts"))
        for p in (players or [])
    )


def _build_stats_fingerprints(box_scores_path):
    """(team_id, contest_id) -> fingerprint of that team's own box-score
    record. Mirrors app.py's _stats_fingerprint exactly, so a duplicate
    contest_id for the same date+opponent is judged the same way here as it
    will be for every future scrape."""
    with open(box_scores_path, encoding='utf-8') as f:
        bs = json.load(f)
    games = bs.get('games', bs) if isinstance(bs, dict) else bs

    fps = {}
    for g in games:
        cid = g.get('contest_id')
        team = g.get('team') or {}
        tid = team.get('team_id')
        if not cid or not tid:
            continue
        # Keyed off whichever SIDE actually has data (mirrors app.py's
        # _stats_fingerprint): MaxPreps sometimes propagates only OUR team's
        # own stats onto a stale duplicate contest_id and leaves the
        # opponent's side empty there, so comparing the whole record would
        # wrongly call that a different game.
        categories = ("shooting", "detailed_shooting", "totals", "misc")
        team_has_data = any((g.get(cat) or {}).get("team", {}).get("players") for cat in categories)
        side = "team" if team_has_data else "opponent"
        parts = []
        for cat in categories:
            block = g.get(cat) or {}
            parts.append((cat, tuple(_players_key((block.get(side) or {}).get("players")))))
        fps[(tid, cid)] = hashlib.sha256(repr(parts).encode('utf-8')).hexdigest()
    return fps


def _build_box_count_lookup(box_scores_path):
    """Same rule as fix_total_games_checked.py: distinct contest_ids per
    (team_id, team_name), used as one of the three signals for TGC."""
    with open(box_scores_path, encoding='utf-8') as f:
        bs = json.load(f)
    games = bs.get('games', bs) if isinstance(bs, dict) else bs

    by_team = defaultdict(set)
    for g in games:
        team = g.get('team') or {}
        tid, tname = team.get('team_id') or '', team.get('team_name') or ''
        cid = g.get('contest_id')
        if tid and cid:
            by_team[(tid, tname)].add(cid)
    return {k: len(v) for k, v in by_team.items()}


def dedupe_team_entry(entry, fps_lookup, team_id, log_lines):
    tagged = []
    for cls, key in BUCKET_KEY.items():
        for g in entry.get(key, {}).get("games", []):
            tagged.append({**g, "_cls": cls})

    groups = defaultdict(list)
    for g in tagged:
        groups[(g.get("date", ""), g.get("opponent_team_id", ""))].append(g)

    kept = []
    for gkey, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue

        with_data = [g for g in group if g["_cls"] != "no_data"]
        no_data   = [g for g in group if g["_cls"] == "no_data"]

        if not with_data:
            best = sorted(group, key=lambda g: PRIORITY[g["_cls"]])[0]
            kept.append(best)
            log_lines.append(f"{entry['teamName']:35s} | {gkey} | "
                              f"{len(group)}x no_data, none with stats — "
                              f"collapsed to 1 (REVIEW MANUALLY)")
            continue

        if len(with_data) > 1:
            missing_fp = [g for g in with_data if not fps_lookup.get(
                (team_id, _contest_id_from_url(g.get("url"))))]
            if missing_fp:
                log_lines.append(f"{entry['teamName']:35s} | {gkey} | "
                                  f"{len(with_data)} entries with stats, but "
                                  f"{len(missing_fp)} not found in the box-scores "
                                  f"file — can't verify sameness, kept ALL "
                                  f"(REVIEW MANUALLY)")

        unique_by_fp = {}
        for g in with_data:
            cid = _contest_id_from_url(g.get("url"))
            fp = fps_lookup.get((team_id, cid)) if cid else None
            fp_key = fp if fp is not None else ("__nofp__", id(g))
            if fp_key not in unique_by_fp:
                unique_by_fp[fp_key] = g
            else:
                prev = unique_by_fp[fp_key]
                log_lines.append(f"{entry['teamName']:35s} | {gkey} | "
                                  f"identical stats across contest_ids "
                                  f"({prev['_cls']} vs {g['_cls']}) — kept 1 of 2")
                if PRIORITY[g["_cls"]] < PRIORITY[prev["_cls"]]:
                    unique_by_fp[fp_key] = g
        kept.extend(unique_by_fp.values())

        for g in no_data:
            log_lines.append(f"{entry['teamName']:35s} | {gkey} | "
                              f"dropped a no_data placeholder — real stats "
                              f"already exist for this date+opponent")

    buckets = {cls: [] for cls in BUCKET_KEY}
    for g in kept:
        cls = g.pop("_cls")
        buckets[cls].append(g)

    for cls, key in BUCKET_KEY.items():
        note = entry.get(key, {}).get("note") or DEFAULT_NOTE[cls]
        entry[key] = {"count": len(buckets[cls]), "note": note, "games": buckets[cls]}

    games_with_stats = len(buckets["full"]) + len(buckets["team_only"]) + len(buckets["opp_only"])
    games_missing = len(buckets["no_data"])
    entry["gamesWithStats"] = games_with_stats
    entry["gamesMissing"] = games_missing
    entry["gamesChecked"] = games_with_stats + games_missing
    return entry


def backfill_gaps(gaps_path, box_scores_path):
    with open(gaps_path, encoding='utf-8') as f:
        gaps = json.load(f)

    fps_lookup = _build_stats_fingerprints(box_scores_path)

    all_entries = []
    for bucket in ("teamsFullBoxScores", "teamsPartialBoxScores", "teamsNoBoxScores"):
        all_entries.extend(gaps.get(bucket, []))

    log_lines = []
    changed = 0
    for entry in all_entries:
        team_id = team_url_to_path(entry.get("teamUrl"))
        before = entry.get("gamesChecked")
        dedupe_team_entry(entry, fps_lookup, team_id, log_lines)
        if entry.get("gamesChecked") != before:
            changed += 1

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

    backup = f"{gaps_path}.bak.{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(gaps_path, backup)
    tmp = gaps_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(gaps, f, indent=2, ensure_ascii=False)
    os.replace(tmp, gaps_path)

    print(f"  Gaps file           : {gaps_path}")
    print(f"  Backup              : {backup}")
    print(f"  Teams with a changed gamesChecked : {changed} / {len(all_entries)}")
    print(f"  Dedupe actions logged             : {len(log_lines)}")
    for line in log_lines:
        print(f"    [DEDUPE] {line}")

    # (team_id -> corrected gamesChecked) for the Final-file fix step.
    gap_count_lookup = {team_url_to_path(e["teamUrl"]): e["gamesChecked"] for e in all_entries}
    return gap_count_lookup, log_lines


def backfill_final(final_path, box_scores_path, gap_count_lookup):
    with open(final_path, encoding='utf-8') as f:
        records = json.load(f)
    if not isinstance(records, list):
        print(f'[ERROR] {final_path}: expected a flat JSON list of records.')
        return

    box_count_lookup = _build_box_count_lookup(box_scores_path)

    updated = []
    bumped, unchanged, no_gap_match = 0, 0, 0
    examples = []
    for r in records:
        if r.get('record_type') != 'team_total':
            updated.append(r)
            continue
        tid = r.get('team_id')
        gp = int(r.get('GP') or 0)
        box_count = box_count_lookup.get((tid, r.get('team_name')), 0)
        gap_count = gap_count_lookup.get(tid)
        if gap_count is None:
            no_gap_match += 1
        target_tgc = max(int(gap_count or 0), int(box_count), gp)
        current = r.get('TotalGamesChecked')

        if target_tgc != current:
            bumped += 1
            if len(examples) < 15:
                examples.append((r.get('team_name'), gp, current, target_tgc, gap_count, box_count))
            if current is None:
                new_rec = {}
                for k, v in r.items():
                    new_rec[k] = v
                    if k == 'GP':
                        new_rec['TotalGamesChecked'] = target_tgc
                updated.append(new_rec)
            else:
                r = {**r}
                r['TotalGamesChecked'] = target_tgc
                updated.append(r)
        else:
            unchanged += 1
            updated.append(r)

    backup = f"{final_path}.bak.{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(final_path, backup)
    tmp = final_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(updated, f, indent=4, ensure_ascii=False)
    os.replace(tmp, final_path)

    print(f"  Final file          : {final_path}")
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
    ap.add_argument('--gaps', required=True)
    ap.add_argument('--box-scores', required=True)
    ap.add_argument('--final', required=True)
    args = ap.parse_args()

    print("=" * 72)
    print("STEP 1: dedupe the gaps file")
    gap_count_lookup, _ = backfill_gaps(args.gaps, args.box_scores)

    print("=" * 72)
    print("STEP 2: recompute TotalGamesChecked on the Final file")
    backfill_final(args.final, args.box_scores, gap_count_lookup)
    print("=" * 72)


if __name__ == '__main__':
    main()
