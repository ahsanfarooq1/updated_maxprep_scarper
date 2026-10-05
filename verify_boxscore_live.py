"""
Verify box-score fetching against LIVE MaxPreps, using the real scraper path.

Run this where MaxPreps is reachable (US region — e.g. the GitHub Actions
workflow .github/workflows/verify-boxscore.yml, or locally behind a
system-wide VPN).

It checks, in order:

  1. Plain HTTP reachability (surfaces the 403 "Geo-block" explicitly).
  2. That ?tab=stats is what exposes the stat tables — the default Recap view
     has none.
  3. That the Next.js RSC payload is present in a PLAIN response. `requests`
     runs no JavaScript, so if the payload parses here it is server-rendered
     and the scraper works as written. This is the one thing a JS-rendering
     proxy cannot confirm.
  4. That scrape_game() returns real players for BOTH teams, via the same
     function the pipeline calls.
  5. A multi-team sweep through a state's real schedule, to confirm this
     holds beyond one hand-picked game.

Exit code 0 = box-score fetching verified. Non-zero = something is broken,
and the output says which stage.

Usage:
    python verify_boxscore_live.py
"""
import sys
import traceback

from bs4 import BeautifulSoup

from scrape_box_scores import (
    HTML_HEADERS,
    _get_opp_index,
    _get_session,
    _parse_rsc_stats,
    _rsc_payload_from_soup,
    _with_stats_tab,
    fetch_schedule,
    get_build_id,
    get_game_entries,
    scrape_game,
    team_url_to_path,
    _short_season,
)

# One known game (Adams City Eagles @ Westminster Wolves, 12-2-2025) that the
# 2026-08-03 pipeline run wrongly recorded as having no data.
GAME_URL = ("https://www.maxpreps.com/co/basketball/game/"
            "adams-city-commerce-city-vs-westminster/12-2-2025/"
            "?c=dc2f2ce6-a427-4c18-93ca-839e288f67a0")
GUID = "dc2f2ce6-a427-4c18-93ca-839e288f67a0"
TEAM_ID = "co/commerce-city/adams-city-eagles/basketball"
TEAM_NAME = "Adams City Eagles"

# Teams whose real schedules are swept in the multi-team stage.
SWEEP = [
    ("https://www.maxpreps.com/co/commerce-city/adams-city-eagles/basketball/",
     "Adams City Eagles", "boys"),
    ("https://www.maxpreps.com/co/greenwood-village/cherry-creek-bruins/basketball/",
     "Cherry Creek Bruins", "boys"),
    ("https://www.maxpreps.com/ut/sandy/alta-hawks/basketball/girls/",
     "Alta Hawks", "girls"),
]

SEASON = "2025-2026"
failures = []


def head(n, title):
    print(f"\n## {n}. {title}\n")


def ok(msg):
    print(f"- PASS — {msg}")


def bad(msg):
    print(f"- **FAIL** — {msg}")
    failures.append(msg)


def main():
    import os
    import re as _re
    import time as _time

    print("# Live box-score verification")
    print(f"\nRun: `{_time.strftime('%Y-%m-%d %H:%M:%S UTC', _time.gmtime())}`"
          f" · runner `{os.environ.get('RUNNER_OS', 'local')}`"
          f" · attempt `{os.environ.get('GITHUB_RUN_NUMBER', '-')}`")
    print(f"\nSeason `{SEASON}` · season URL suffix `{_short_season(SEASON)}`")

    # Where are we exiting from? A block is usually keyed on this.
    try:
        who = _get_session().get("https://ipinfo.io/json", timeout=20).json()
        print(f"\nExit IP `{who.get('ip')}` · {who.get('country')} "
              f"{who.get('city')} · org `{who.get('org')}`")
    except Exception as e:
        print(f"\n(could not determine exit IP: {e})")

    # ── 1. Reachability ──────────────────────────────────────────────────
    head(1, "Plain HTTP reachability")
    r = _get_session().get(GAME_URL, headers=HTML_HEADERS, timeout=30,
                           allow_redirects=True)
    print(f"- status `{r.status_code}`, {len(r.text):,} bytes")
    if r.status_code != 200:
        title = _re.search(r"<title[^>]*>(.*?)</title>", r.text,
                           _re.I | _re.S)
        print(f"- page title: `{title.group(1).strip() if title else '(none)'}`")
        print("- response headers:")
        for k, v in r.headers.items():
            print(f"    - `{k}: {v}`")
        print("\n```\n" + r.text[:700] + "\n```")
        low = r.text.lower()
        if "geo-block" in low:
            bad("GEO-BLOCKED (403) — this exit IP's country is not allowed.")
        elif r.status_code == 406:
            bad("406 Not Acceptable. Reverting the request headers did NOT "
                "clear this, so it is keyed on the exit IP, not the headers "
                "— MaxPreps appears to reject datacenter/cloud ranges "
                "(GitHub runners are Azure). CI cannot verify or scrape; "
                "only a residential/VPN exit in an allowed country works.")
        else:
            bad(f"unexpected status {r.status_code}")
        return
    ok("MaxPreps reachable")

    # ── 2. ?tab=stats is required ────────────────────────────────────────
    head(2, "`?tab=stats` exposes the stat tables")
    default_soup = BeautifulSoup(r.text, "html.parser")
    default_groups = _parse_rsc_stats(default_soup)
    stats_url = _with_stats_tab(r.url)
    r2 = _get_session().get(stats_url, headers=HTML_HEADERS, timeout=30,
                            allow_redirects=True)
    stats_soup = BeautifulSoup(r2.text, "html.parser")
    stats_groups = _parse_rsc_stats(stats_soup)
    print(f"- default view : {len(default_groups)} school(s) with stats")
    print(f"- `?tab=stats` : {len(stats_groups)} school(s) with stats")
    if stats_groups:
        ok("stats tab yields stat tables")
    else:
        bad("no stat tables even with ?tab=stats — parser or page changed")

    # ── 3. RSC payload present WITHOUT JavaScript ───────────────────────
    head(3, "RSC payload is server-rendered (no JS executed)")
    payload = _rsc_payload_from_soup(stats_soup)
    print(f"- reconstructed payload: {len(payload):,} chars")
    if len(payload) > 1000:
        ok("payload present in a plain `requests` response — "
           "server-rendered, so no browser is needed")
    else:
        bad("RSC payload missing from the plain response. The parser's "
            "primary path cannot work with `requests`; it would fall back "
            "to HTML tables (one team per game only).")

    # ── 4. Both teams parsed via the real scraper entry point ───────────
    head(4, "`scrape_game()` returns both teams' players")
    for school, cats in stats_groups.items():
        print(f"- `{school}`: " + ", ".join(f"{c}={len(p)}"
                                            for c, p in sorted(cats.items())))
    opp_index = _get_opp_index("boys", SEASON)
    rec = scrape_game(GAME_URL, GUID, None, TEAM_NAME, TEAM_ID, opp_index)
    if not rec or (isinstance(rec, dict) and rec.get("_404")):
        bad(f"scrape_game returned {rec!r}")
    else:
        ours = rec["shooting"]["team"]["players"]
        theirs = rec["shooting"]["opponent"]["players"]
        print(f"- team    : `{rec['team']['team_id']}` -> {len(ours)} shooters")
        print(f"- opponent: `{rec['opponent']['team_id']}` -> {len(theirs)} shooters")
        if ours:
            print(f"- sample  : `{ours[0]}`")
        if ours and theirs:
            ok("both sides populated with real players")
        elif ours:
            bad("opponent side empty — RSC path likely not used")
        else:
            bad("our side empty")
        if "/" not in (rec["opponent"]["team_id"] or ""):
            bad(f"opponent id not canonical: {rec['opponent']['team_id']!r}")
        else:
            ok(f"opponent id canonical: `{rec['opponent']['team_id']}`")

    # ── 5. Multi-team sweep over real schedules ─────────────────────────
    head(5, "Multi-team sweep (first 3 games of each team's real schedule)")
    bid, _ = get_build_id()
    print(f"- build id `{bid}`\n")
    suffix = _short_season(SEASON)
    grand_games = 0
    for team_url, team_name, sport in SWEEP:
        path = team_url_to_path(team_url)
        idx = _get_opp_index(sport, SEASON)
        try:
            contests = fetch_schedule(bid, path, season_suffix=suffix)
            if isinstance(contests, dict) or not contests:
                bad(f"{team_name}: schedule fetch returned {contests!r:.60}")
                continue
            entries = [e for e in get_game_entries(contests) if e[1]]
            print(f"- **{team_name}** ({sport}): {len(entries)} scheduled games")
            found = 0
            for game_url, guid, ssid in entries[:3]:
                got = scrape_game(game_url, guid, ssid, team_name, path, idx)
                if got and not (isinstance(got, dict) and got.get("_404")):
                    t = len(got["shooting"]["team"]["players"])
                    o = len(got["shooting"]["opponent"]["players"])
                    print(f"    - {got['game_date']} vs "
                          f"`{got['opponent']['team_id']}` — team={t} opp={o}")
                    if t or o:
                        found += 1
                else:
                    print(f"    - {game_url[-46:]} — no stat data")
            grand_games += found
            print(f"    -> {found}/{min(3, len(entries))} games with player stats")
        except Exception as e:
            bad(f"{team_name}: {type(e).__name__}: {e}")
            traceback.print_exc()

    if grand_games:
        ok(f"{grand_games} games returned player stats across the sweep")
    else:
        bad("no games returned player stats in the sweep")

    # ── Verdict ─────────────────────────────────────────────────────────
    print("\n## Verdict\n")
    if failures:
        print(f"**FAILED** — {len(failures)} problem(s):\n")
        for f in failures:
            print(f"- {f}")
    else:
        print("**Box-score fetching is working.** The RSC payload is "
              "server-rendered, `?tab=stats` exposes it, and both teams' "
              "players parse out of a plain `requests` fetch.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\n## Verdict\n\n**CRASHED**\n\n```")
        traceback.print_exc()
        print("```")
        sys.exit(2)
    sys.exit(1 if failures else 0)
