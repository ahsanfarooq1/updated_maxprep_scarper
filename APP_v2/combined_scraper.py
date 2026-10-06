"""
combined_scraper.py — merges the current pipeline's Stage 1 (gap finder,
app.py) and Stage 3 (box-score scraper, scrape_box_scores.py) into ONE pass.

THE PROBLEM THIS FIXES
-----------------------
Today, every game's box-score page is fetched and parsed TWICE:
  - once by app.py's check_game_worker/_classify_game, which parses the
    full page just to decide full/team_only/opp_only/no_data, then
    THROWS AWAY everything except a thin summary (date, opponent, a stats
    fingerprint) — the actual player stats it just parsed are discarded.
  - once by scrape_box_scores.py's scrape_game, which re-fetches the exact
    same URL moments/stages later to get that same content again, this
    time keeping it, to build the box-score record.
Every team's schedule is also fetched twice (once in each stage's Phase 1).

THE FIX
-------
Fetch each game's page once, parse it once, and build BOTH outputs from
that single `page` dict — a classification record (today's
*_data_gaps_*.json shape) AND a box-score record (today's
*_box_scores_*.json shape) — at the same time.

WHAT'S REUSED, NOT REWRITTEN
----------------------------
This file imports and calls the existing, already-hardened functions in
app.py and scrape_box_scores.py rather than re-implementing them:
  - HTTP fetching: scrape_box_scores._http_get_page, _with_stats_tab
  - Page parsing:  scrape_box_scores.parse_game_page
  - Classification + fingerprint: the same logic app.py._classify_game
    uses (has_team/has_opp → full/team_only/opp_only/no_data), inlined
    here only because it needs to ALSO keep `page` instead of discarding
    it — not because the logic itself changed.
  - Dedup across duplicate contest_ids: app.py._dedupe_games_by_date_opponent
  - Schedule fetch + overall W-L record: app.py.fetch_sched_worker
  - Build-id management, opponent index, level handling: reused as-is

OUTPUT FILES are byte-for-byte the same SHAPE as today's gaps/box-scores
files, so Stage 4 (Accumulation_data.py, merge_all_stats_tab.py,
fix_total_games_checked.py) and every downstream consumer work against
this completely unchanged.

Usage:
  python combined_scraper.py --state OR --sport boys --season 2026-2027 \
      --gaps-output path/to/or_data_gaps_boys_2026_2027.json \
      --box-output  path/to/or_box_scores_boys_2026_2027.json
"""

import os
import re
import sys
import json
import time
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import app as gapfinder          # noqa: E402  (must follow sys.path insert)
import scrape_box_scores as bx   # noqa: E402

_original_print = print
def print(*args, **kwargs):  # noqa: A001
    _original_print(time.strftime('[%Y-%m-%d %H:%M:%S]'), *args, **kwargs)

TEAM_WORKERS = 15   # parallel teams; games within a team run sequentially,
                    # same rate-limiting rationale as scrape_box_scores.py


def _build_box_record(our_team_id, our_team_name, guid, final_url, date, page):
    """Exact same shape scrape_box_scores.scrape_game() produces, built
    from an already-parsed `page` instead of a second fetch."""
    return {
        "contest_id":        guid,
        "game_url":          final_url,
        "game_date":         date,
        "is_deleted":        False,
        "team":     {"team_id": our_team_id,  "team_name": our_team_name},
        "opponent": {"team_id": page["opp_id"], "team_name": page["opp_name"]},
        "shooting":          page["shooting"],
        "detailed_shooting": page["detailed_shooting"],
        "totals":            page["totals"],
        "misc":              page["misc"],
    }


def classify_and_record(soup, final_url, our_team_name, our_team_id, opp_index, guid):
    """One parse -> (classification_dict, box_record_or_None).

    classification_dict matches app.py's _classify_game output exactly
    (same keys, same has_team/has_opp -> full/team_only/opp_only/no_data
    logic, same stats_fingerprint). box_record is None for no_data (mirrors
    scrape_game() returning None for an unparseable/empty page), otherwise
    the same shape scrape_game() builds.
    """
    date_m = re.search(r"/(\d{1,2}-\d{1,2}-\d{4})/", final_url or "")
    date = date_m.group(1) if date_m else ""

    page = bx.parse_game_page(soup, final_url, our_team_name, our_team_id, opp_index)
    if page is None:
        # No recognisable stat table anywhere on the page at all. Reuse
        # app.py's own fallback path verbatim (page-hyperlink opponent
        # lookup) so this edge case is handled identically to today.
        classify = gapfinder._classify_game(soup, final_url, our_team_name, our_team_id, opp_index)
        return classify, None

    has_team = any(len(page[c]["team"]["players"]) > 0
                   for c in ("shooting", "detailed_shooting", "totals", "misc"))
    has_opp = any(len(page[c]["opponent"]["players"]) > 0
                  for c in ("shooting", "detailed_shooting", "totals", "misc"))
    if has_team and has_opp:
        cls = "full"
    elif has_team:
        cls = "team_only"
    elif has_opp:
        cls = "opp_only"
    else:
        cls = "no_data"

    classify = {
        "classification":     cls,
        "date":               date,
        "url":                final_url,
        "opponent_team_id":   page["opp_id"],
        "opponent_team_name": page["opp_name"],
        "stats_fingerprint":  gapfinder._stats_fingerprint(page) if cls != "no_data" else None,
    }

    if cls == "no_data":
        return classify, None

    record = _build_box_record(our_team_id, our_team_name, guid, final_url, date, page)
    return classify, record


def combined_game_worker(game_url, guid, ssid, team_name, team_id, opp_index):
    """Fetch ONE game's page ONCE. Replaces app.py's check_game_worker +
    scrape_box_scores.py's scrape_game — same HTTP fetch chain as both
    (shared already via bx._http_get_page / bx._with_stats_tab), just
    called once instead of twice.

    Returns (classification_dict_or_None, box_record_or_None).
    """
    time.sleep(gapfinder.DELAY)
    url = (f"https://www.maxpreps.com/local/stats/boxscore.aspx?contestid={guid}&ssid={ssid}"
           if guid and ssid else game_url)
    try:
        status, html, final_url = bx._http_get_page(url, timeout=20, allow_redirects=True)
        if status != 200 or not html:
            return None, None

        stats_url = bx._with_stats_tab(final_url)
        if stats_url != final_url:
            time.sleep(gapfinder.DELAY)
            st2, html2, final2 = bx._http_get_page(stats_url, timeout=20, allow_redirects=True)
            if st2 == 200 and html2:
                html, final_url = html2, (final2 or stats_url)

        soup = BeautifulSoup(html, "html.parser")

        # Anchor-derived entries carry no guid; recover it from the
        # resolved URL, same as scrape_game() does.
        if not guid:
            cm = re.search(r"[?&]c=([A-Za-z0-9_-]+)", final_url or "")
            if cm:
                guid = gapfinder.decode_contest_guid(cm.group(1))

        return classify_and_record(soup, final_url, team_name, team_id, opp_index, guid)
    except Exception:
        return None, None


def _build_combined_cache(existing_gaps, existing_box):
    """(team_id, contest_id) -> (classification, game_rec, box_record) for
    every game an earlier run already found real stats for. Mirrors
    app.py._build_game_cache + scrape_box_scores.py's box-score cache,
    combined into one lookup so a daily re-run skips BOTH the
    classification fetch and the box-score fetch for a game it already
    has, not just one of them.

    no_data games are deliberately excluded - always re-checked live,
    since an unplayed/unstatted game today can have a result tomorrow.
    """
    box_by_key = {}
    if existing_box:
        games = existing_box.get("games", existing_box) if isinstance(existing_box, dict) else existing_box
        for g in games:
            tid = g.get("team", {}).get("team_id")
            cid = g.get("contest_id")
            if tid and cid:
                box_by_key[(tid, cid)] = g

    cache = {}
    if existing_gaps:
        for bucket in ("teamsFullBoxScores", "teamsPartialBoxScores", "teamsNoBoxScores"):
            for team in existing_gaps.get(bucket, []):
                t_id = gapfinder.team_url_to_path(team.get("teamUrl", ""))
                for cls, key in (("full", "fullDataGames"), ("team_only", "teamOnlyDataGames"),
                                 ("opp_only", "opponentOnlyDataGames")):
                    for g in team.get(key, {}).get("games", []):
                        cid = gapfinder._contest_id_from_url(g.get("url"))
                        if cid:
                            box_rec = box_by_key.get((t_id, cid))
                            cache[(t_id, cid)] = (cls, g, box_rec)
    return cache


def process_team(job):
    """One team: fetch schedule once, then walk every game, fetching each
    page at most once (cache hits aside). Returns everything needed to
    build BOTH output files for this team."""
    team, entries, overall_record, cache = (
        job["team"], job["entries"], job["overall_record"], job["cache"]
    )
    t_id = gapfinder.team_url_to_path(team["teamUrl"])
    t_name = team["teamName"]

    full_games, team_only_games, opp_only_games, no_data_games = [], [], [], []
    box_records = []

    for url, guid, ssid in entries:
        cached = cache.get((t_id, guid)) if guid else None
        if cached is not None:
            cls, game_rec, box_rec = cached
        else:
            classify, box_rec = combined_game_worker(url, guid, ssid, t_name, t_id, job["opp_index"])
            if classify is None:
                # Transient fetch failure for this one game - same silent-skip
                # behavior the old app.py always had, just logged now so a
                # missing game is diagnosable without a forensic file diff.
                # Not cached, so a future incremental run will retry it.
                print(f"  [WARN] {t_name}: fetch failed for one game, skipping "
                      f"(will retry next run) - {url}")
                continue
            cls = classify.pop("classification", "no_data")
            game_rec = classify

        if   cls == "full":      full_games.append(game_rec)
        elif cls == "team_only": team_only_games.append(game_rec)
        elif cls == "opp_only":  opp_only_games.append(game_rec)
        else:                    no_data_games.append(game_rec)

        if box_rec is not None:
            box_records.append(box_rec)

    # Same duplicate-contest-id dedup used in production today - reused
    # verbatim, not reimplemented.
    full_games, team_only_games, opp_only_games, no_data_games = \
        gapfinder._dedupe_games_by_date_opponent(full_games, team_only_games,
                                                  opp_only_games, no_data_games, t_name)

    # Keep box_records consistent with whatever survived dedup above -
    # drop any box record whose contest_id didn't make the cut.
    surviving_cids = set()
    for bucket in (full_games, team_only_games, opp_only_games):
        for g in bucket:
            cid = gapfinder._contest_id_from_url(g.get("url"))
            if cid:
                surviving_cids.add(cid)
    box_records = [r for r in box_records if r.get("contest_id") in surviving_cids]

    games_with_stats = len(full_games) + len(team_only_games) + len(opp_only_games)
    games_missing = len(no_data_games)
    games_checked = games_with_stats + games_missing
    if overall_record is not None and overall_record >= games_with_stats:
        games_checked = overall_record
        games_missing = max(0, games_checked - games_with_stats)

    gaps_entry = {
        "teamName":       t_name,
        "teamUrl":        team["teamUrl"],
        "region":         team["region"],
        "gamesChecked":   games_checked,
        "gamesWithStats": games_with_stats,
        "gamesMissing":   games_missing,
        "recordGamesPlayed": overall_record,
        "fullDataGames": {"count": len(full_games), "note": "Both teams entered stats.", "games": full_games},
        "teamOnlyDataGames": {"count": len(team_only_games), "note": "Only THIS team entered stats; opponent did not.", "games": team_only_games},
        "opponentOnlyDataGames": {"count": len(opp_only_games), "note": "Only the OPPONENT entered stats; this team did not.", "games": opp_only_games},
        "noDataGames": {"count": len(no_data_games), "note": "NEITHER team entered any stats.", "games": no_data_games},
    }
    return team, gaps_entry, box_records


def run(state_code, sport, season, level="varsity", workers=TEAM_WORKERS,
        gaps_output=None, box_output=None, limit=None):
    state_lower = state_code.lower()
    state_name = gapfinder.STATE_NAMES.get(state_code, state_code)
    season_fn = season.replace("-", "_")
    lvl_sfx = bx.level_file_suffix(bx.normalise_level(level))

    if gaps_output is None:
        gaps_output = os.path.join(gapfinder.DATA_DIR, f"{state_lower}_data_gaps_{sport}{lvl_sfx}_{season_fn}.json")
    if box_output is None:
        box_output = os.path.join(gapfinder.DATA_DIR, f"{state_lower}_box_scores_{sport}{lvl_sfx}_{season_fn}.json")

    # Team enumeration - same master-list loading as app.py's main().
    input_file = os.path.join(REPO_ROOT, f"{sport}_basketball_all_states_{season}.json")
    if not os.path.exists(input_file):
        input_file = os.path.join(REPO_ROOT, f"{sport}_basketball_all_states.json")
    with open(input_file, encoding="utf-8") as f:
        data = json.load(f)
    if state_code not in data.get("byState", {}):
        print(f"Error: State {state_code} not found in master team list.")
        sys.exit(1)

    level_norm = bx.normalise_level(level)
    seen_urls = set()
    all_teams = []
    for r, d in data["byState"][state_code]["regions"].items():
        for t in d["teams"]:
            url = t.get("teamUrl", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            all_teams.append({
                "teamName": gapfinder.name_from_url(url, t.get("teamName", "")),
                "teamUrl": bx.apply_level(url, level_norm),
                "region": r,
            })
    if limit and limit > 0:
        all_teams = all_teams[:limit]
        print(f"[--limit {limit}] Only the first {limit} team(s) will be processed.")
    total = len(all_teams)
    print(f"Teams enumerated: {total}")

    existing_gaps, existing_box = None, None
    if os.path.exists(gaps_output):
        try:
            with open(gaps_output, encoding="utf-8") as f:
                existing_gaps = json.load(f)
        except Exception as e:
            print(f"  [WARN] Could not load existing gaps file: {e}")
    if os.path.exists(box_output):
        try:
            with open(box_output, encoding="utf-8") as f:
                existing_box = json.load(f)
        except Exception as e:
            print(f"  [WARN] Could not load existing box-scores file: {e}")

    cache = _build_combined_cache(existing_gaps, existing_box)
    print(f"Loaded {len(cache)} already-checked games from existing files - "
          f"only new or still-unresolved games will be (re-)fetched.")

    season_suffix = gapfinder._short_season(season)
    opp_index = bx._get_opp_index(sport, season)

    print(f"Phase 1: Fetching {total} schedules ({gapfinder.SCHED_WORKERS} workers)...")
    sched_results = {}
    with ThreadPoolExecutor(max_workers=gapfinder.SCHED_WORKERS) as pool:
        futures = {pool.submit(gapfinder.fetch_sched_worker, t, season_suffix): t for t in all_teams}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                team, entries, overall_record = fut.result()
            except Exception as e:
                orig = futures[fut]
                print(f"  [WARN] Schedule worker crashed for {orig['teamName']}: {e}")
                sched_results[orig["teamUrl"]] = (orig, None, None)
                continue
            sched_results[team["teamUrl"]] = (team, entries, overall_record)
            if i % 100 == 0 or i == total:
                print(f"  Schedules: {i}/{total} done")

    print(f"Phase 2: Fetching + classifying games in parallel ({workers} teams at a time)...")
    full_data, partial_data, no_data = [], [], []
    all_games = []
    errors = []
    jobs = []
    processed_team_ids = set()
    for turl, (team, entries, overall_record) in sched_results.items():
        if entries is None:
            errors.append({"teamName": team["teamName"], "teamUrl": team["teamUrl"], "region": team["region"]})
        elif not entries:
            city_m = re.search(rf"/{state_lower}/([^/]+)/", team["teamUrl"])
            city = city_m.group(1).replace("-", " ").title() if city_m else state_name
            no_data.append({"teamName": team["teamName"], "teamUrl": team["teamUrl"], "region": team["region"],
                             "gamesChecked": 0, "gamesWithStats": 0, "gamesMissing": 0,
                             "alternativeSources": {
                                 "scoreStream": gapfinder.scorestream_url(team["teamName"], state_name),
                                 "googleSearch": gapfinder.google_search_url(team["teamName"], city, state_name),
                             }})
            processed_team_ids.add(gapfinder.team_url_to_path(team["teamUrl"]))
        else:
            jobs.append({"team": team, "entries": entries, "overall_record": overall_record,
                         "cache": cache, "opp_index": opp_index})

    done = 0
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_team, j): j for j in jobs}
        for fut in as_completed(futures):
            try:
                team, gaps_entry, box_records = fut.result()
            except Exception as e:
                j = futures[fut]
                print(f"  [ERROR] process_team crashed for {j['team'].get('teamName','?')}: {e}")
                continue
            gc, gm, gws = gaps_entry["gamesChecked"], gaps_entry["gamesMissing"], gaps_entry["gamesWithStats"]
            with lock:
                if gc == 0:          no_data.append(gaps_entry)
                elif gm == 0:        full_data.append(gaps_entry)
                elif gws > 0:        partial_data.append(gaps_entry)
                else:                no_data.append(gaps_entry)
                all_games.extend(box_records)
                processed_team_ids.add(gapfinder.team_url_to_path(team["teamUrl"]))
                done += 1
                # Same line shape app.py's old Phase 2 used (`[N/M] .. Full:
                # X | Part: Y | TeamName`) so streamlit_app.py's existing log
                # parser keeps working unmodified - this stage now does what
                # used to be split across gap-finder Phase 2 AND the
                # standalone box-score stage, so it's correctly reported as
                # the same UI phase those used to be.
                pct = done / len(jobs) * 100 if jobs else 0.0
                print(f"  [{done:>4}/{len(jobs)}] {pct:5.1f}% | Full: {len(full_data):>4} | "
                      f"Part: {len(partial_data):>4} | {team['teamName']}")

    # Write gaps file (same shape as app.py._save_gaps produces).
    total_games_checked = sum(t["gamesChecked"] for t in full_data + partial_data + no_data)
    gaps_out = {
        "meta": {
            "state": state_name, "stateCode": state_code,
            "sport": f"{sport.title()} Basketball", "season": season,
            "totalTeams": total, "processedTeamsCount": len(processed_team_ids),
            "processedTeams": sorted(processed_team_ids),
            "totalGamesChecked": total_games_checked,
            "teamsFullBoxScores": len(full_data), "teamsPartialBoxScores": len(partial_data),
            "teamsNoBoxScores": len(no_data), "errors_count": len(errors),
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "teamsFullBoxScores": sorted(full_data, key=lambda x: x["teamName"]),
        "teamsPartialBoxScores": sorted(partial_data, key=lambda x: x["teamName"]),
        "teamsNoBoxScores": sorted(no_data, key=lambda x: x["teamName"]),
        "errors": errors,
    }
    os.makedirs(os.path.dirname(os.path.abspath(gaps_output)), exist_ok=True)
    tmp = gaps_output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(gaps_out, f, indent=2, ensure_ascii=False)
    os.replace(tmp, gaps_output)

    # Write box-scores file (same shape as scrape_box_scores.py._save produces).
    box_out = {
        "meta": {
            "totalGames": len(all_games), "totalErrors": len(errors), "totalTeams": total,
            "processedTeamsCount": len(processed_team_ids),
            "processedTeams": sorted(processed_team_ids),
            "errors": errors,
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "games": all_games,
    }
    tmp = box_output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(box_out, f, indent=2, ensure_ascii=False)
    os.replace(tmp, box_output)

    print(f"Done. Gaps -> {gaps_output}")
    print(f"      Box scores -> {box_output} ({len(all_games)} records)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", required=True)
    ap.add_argument("--sport", required=True, choices=["boys", "girls"])
    ap.add_argument("--season", required=True)
    ap.add_argument("--level", default="varsity", choices=["varsity", "jv", "freshman"])
    ap.add_argument("--workers", type=int, default=TEAM_WORKERS)
    ap.add_argument("--gaps-output", default=None)
    ap.add_argument("--box-output", default=None)
    ap.add_argument("--limit", type=int, default=None,
                    help="Process only the first N teams - for a quick smoke test.")
    args = ap.parse_args()
    run(args.state.upper(), args.sport, args.season, level=args.level, workers=args.workers,
        gaps_output=args.gaps_output, box_output=args.box_output, limit=args.limit)


if __name__ == "__main__":
    main()
