"""
Create a minimal live test:
1. Fetch the Allen vs Dallas Jesuit game with ?tab=Stats (VPN required)
2. Dump the RSC payload to a file for analysis
3. Try the current scrape_box_scores parsing on it
4. Check what season suffix the schedule API actually returns games for
"""
import re
import json
import time
import sys
import requests
from bs4 import BeautifulSoup
from scrape_box_scores import (
    scrape_game, parse_game_page, _parse_rsc_stats, _rsc_payload_from_soup,
    _get_session, HTML_HEADERS, _with_stats_tab, _get_opp_index,
    fetch_schedule, get_build_id, get_game_entries, _short_season
)

# Allen (TX) vs Dallas Jesuit - the URL the user gave
TEST_URL = (
    "https://www.maxpreps.com/tx/basketball/game/"
    "allen-vs-dallas-jesuit/11-14-2025/"
    "?c=5dfde36d-7b8e-485d-9473-f57c74d44cc6&tab=Stats"
)
TEST_GUID = "5dfde36d-7b8e-485d-9473-f57c74d44cc6"
TEAM_ID   = "tx/allen/allen-eagles/basketball"
TEAM_NAME = "Allen Eagles"

# Adams City vs Westminster (CO) — from test_live_box_score.py
CO_URL    = ("https://www.maxpreps.com/co/basketball/game/"
             "adams-city-commerce-city-vs-westminster/12-2-2025/"
             "?c=dc2f2ce6-a427-4c18-93ca-839e288f67a0")
CO_GUID   = "dc2f2ce6-a427-4c18-93ca-839e288f67a0"
CO_TEAM   = "co/commerce-city/adams-city-eagles/basketball"
CO_NAME   = "Adams City Eagles"

def test_game(url, guid, team_id, team_name, sport, season):
    print(f"\n--- Testing: {team_name} ---")
    print(f"    URL: {url[:80]}")
    
    sess = _get_session()
    r = sess.get(url, headers=HTML_HEADERS, timeout=30, allow_redirects=True)
    print(f"    Initial HTTP status: {r.status_code}  final_url={r.url[:90]}")
    
    if r.status_code != 200:
        print(f"    BLOCKED or error: {r.text[:200]}")
        return
    
    # Re-fetch with tab=stats
    stats_url = _with_stats_tab(r.url)
    if stats_url != r.url:
        time.sleep(0.5)
        r2 = sess.get(stats_url, headers=HTML_HEADERS, timeout=30, allow_redirects=True)
        print(f"    tab=stats status: {r2.status_code}")
        if r2.status_code == 200:
            r = r2
    
    soup = BeautifulSoup(r.text, "html.parser")
    
    # RSC payload
    payload = _rsc_payload_from_soup(soup)
    print(f"    RSC payload size: {len(payload)} chars")
    
    rsc_dump_file = f"rsc_dump_{team_name.replace(' ', '_')}.txt"
    with open(rsc_dump_file, "w", encoding="utf-8") as f:
        f.write(payload)
    print(f"    RSC payload saved to: {rsc_dump_file}")
    
    if payload:
        # Check for stat groups
        groups = re.findall(r'"name":"([^"]{1,40})"[^}]*"subgroups":', payload)
        print(f"    Stat group names: {groups[:10]}")
        
        # Check specifically for the group pattern our code uses
        from scrape_box_scores import _RSC_GROUP_RE, _balanced_json_at
        matches = list(_RSC_GROUP_RE.finditer(payload))
        print(f"    _RSC_GROUP_RE matches: {len(matches)}")
        if matches:
            for m in matches[:3]:
                raw = _balanced_json_at(payload, m.start())
                if raw:
                    try:
                        group = json.loads(raw)
                        subs = group.get('subgroups', [])
                        print(f"      Group '{group.get('name')}': {len(subs)} subgroups")
                        for sub in subs[:2]:
                            stats = sub.get('stats', {})
                            rows = stats.get('rows', [])
                            cols = stats.get('columns', [])
                            print(f"        Subgroup: cols={len(cols)} rows={len(rows)}")
                            if cols:
                                headers = [c.get('header', '') for c in cols]
                                print(f"        Headers: {headers}")
                    except Exception as e:
                        print(f"      Parse error: {e}")
        
        # Now try actual parser
        rsc_stats = _parse_rsc_stats(soup)
        print(f"    _parse_rsc_stats result keys: {list(rsc_stats.keys())}")
    else:
        print("    !! NO RSC payload found — checking for new delivery method")
        
        # Check if stats are loaded via a separate API call (not RSC)
        # Look for any data-loading script patterns
        scripts = soup.find_all("script")
        for s in scripts[:20]:
            text = s.string or ""
            if any(kw in text for kw in ["contestId", "statsData", "boxScore", "playerStats", "graphql"]):
                print(f"    Found interesting script: {text[:300]}")
        
        # Check __NEXT_DATA__
        nd_script = soup.find("script", id="__NEXT_DATA__")
        if nd_script:
            nd = json.loads(nd_script.string or "{}")
            print(f"    __NEXT_DATA__ keys: {list(nd.keys())}")
            pp = nd.get("props", {}).get("pageProps", {})
            print(f"    pageProps keys: {list(pp.keys())[:10]}")
        
        # Check for any fetch() or XMLHttpRequest patterns in scripts
        for s in scripts:
            text = s.string or ""
            if "fetch(" in text or "api." in text.lower():
                if len(text) > 50:
                    print(f"    Fetch/API script snippet: {text[:200]}")
                    break
    
    # Full scrape_game test
    opp_idx = _get_opp_index(sport, season)
    record = scrape_game(url, guid, ssid=None, our_team_name=team_name,
                         team_id=team_id, opp_index=opp_idx)
    if record and not record.get("_404"):
        print(f"    scrape_game SUCCESS!")
        for cat in ("shooting", "detailed_shooting", "totals", "misc"):
            t = len(record[cat]["team"]["players"])
            o = len(record[cat]["opponent"]["players"])
            print(f"      {cat}: team={t} opp={o}")
    else:
        print(f"    scrape_game returned: {record!r}")

def test_schedule_seasons(team_path, seasons):
    print(f"\n--- Schedule season test for: {team_path} ---")
    bid, _ = get_build_id()
    print(f"    Build ID: {bid}")
    for s in seasons:
        suffix = _short_season(s) if s else None
        contests = fetch_schedule(bid, team_path, season_suffix=suffix)
        if isinstance(contests, dict):
            print(f"    Season {s or '(current)'}: {contests}")
        elif contests is None:
            print(f"    Season {s or '(current)'}: None (error)")
        else:
            print(f"    Season {s or '(current)'}: {len(contests)} contests found")
            if contests:
                entries = get_game_entries(contests)
                print(f"      Game entries (url, guid, ssid): {len(entries)}")
                if entries:
                    print(f"      First entry URL: {entries[0][0][:80]}")

if __name__ == "__main__":
    print("="*60)
    print("Live MaxPreps Box Score Analysis")
    print("="*60)
    
    # Test TX game
    test_game(TEST_URL, TEST_GUID, TEAM_ID, TEAM_NAME, "boys", "2025-2026")
    
    # Test CO game
    test_game(CO_URL, CO_GUID, CO_TEAM, CO_NAME, "boys", "2025-2026")
    
    # Test what season the schedule API returns for CO team
    test_schedule_seasons(
        "co/commerce-city/adams-city-eagles/basketball",
        ["2025-2026", "2026-2027", None]
    )
    
    # Test TX Allen schedule
    test_schedule_seasons(
        "tx/allen/allen-eagles/basketball",
        ["2025-2026", "2026-2027", None]
    )
