# MaxPreps Statewise Scraper

Scrapes high-school basketball box scores from MaxPreps for a whole state and
season, and accumulates them into one per-player season stat file.

## ⚠️ MaxPreps geo-blocks some countries

MaxPreps returns a `403 - Geo-block` page to requests from certain countries.
This is **genuinely geographic**, not bot detection:

* A VPN with an allowed exit IP fixes it — for browsers *and* scripts.
* Without one, even a real headless Chrome is blocked.
* Header tweaks and TLS impersonation (`curl_cffi`) do **not** help.

A **browser VPN extension is not enough** — it only routes the browser's own
traffic, so Python still exits via your real IP. Use a **system-wide VPN
client** (or run the scraper on a host in an allowed region).

Quick check that the scraper can reach MaxPreps:

```bash
python test_live_box_score.py
```

It prints the raw HTTP status and says plainly if you're geo-blocked.

## HTTP transport

MaxPreps answers plain-`requests` traffic with **406 Not Acceptable** in some
environments (its TLS fingerprint is recognisable), so fetches go through a
chain, first available wins:

```
curl_cffi (Chrome TLS impersonation)  →  system curl  →  plain requests
```

`curl_cffi` is in `requirements.txt`; if it's missing the scraper still runs on
system `curl`, and finally on `requests`. The active chain is printed at
startup (`HTTP transport : …`). A 403/404 is returned immediately rather than
retried across backends — those are real answers, not fingerprint rejections.

## Team levels: varsity / JV / freshman

MaxPreps serves varsity at the bare team URL and nests the other levels **after
the gender**, before the season:

```
boys varsity     /tx/allen/allen-eagles/basketball/25-26/schedule/
boys JV          /tx/allen/allen-eagles/basketball/jv/25-26/schedule/
boys freshman    /tx/allen/allen-eagles/basketball/freshman/25-26/schedule/
girls JV         /tx/allen/allen-eagles/basketball/girls/jv/25-26/schedule/
```

`/jv/girls/` (the reverse order) is a 404 — gender always precedes level.

Pass `--level varsity|jv|freshman` (default `varsity`) to
`APP_v2/run_pipeline_v2.py`, `APP_v2/combined_scraper.py`,
`accumulate_from_stats_tab.py`, or pick it from the **Level** dropdown in
the Streamlit UI:

```bash
python APP_v2/run_pipeline_v2.py --state CO --sport boys --season 2025-2026 --level jv
```

**Output files.** Varsity filenames are unchanged, so existing varsity data and
any downstream consumers keep working. JV and freshman get their own set:

```
co_box_scores_boys_2025_2026.json            # varsity
co_box_scores_boys_jv_2025_2026.json         # JV
co_box_scores_boys_freshman_2025_2026.json   # freshman
```

Team ids carry the level too (`co/westminster/westminster-wolves/basketball/jv`),
for both the scraped team and its opponent — a JV game is JV for both sides — so
levels never collide in the accumulated output.

**Expect thinner data at these levels.** Many schools only upload varsity stats.
Of six programs sampled, all six had varsity stats; only two had JV and one had
freshman. Teams with no stats simply produce empty player lists, exactly as they
do for varsity.

## Pipeline

`APP_v2/run_pipeline_v2.py` is the engine used by default (Streamlit calls
this). It runs 3 stages — Stage A and Stage B run **concurrently**:

| Stage | Script | Output | Notes |
|---|---|---|---|
| A | `APP_v2/combined_scraper.py` | `{state}_data_gaps_{sport}_{season}.json` + `{state}_box_scores_{sport}_{season}.json` | Fetches each game's box-score page **once**, building the classification record and the box-score record from the same parse — replaces what used to be two separate stages (gap finder + box scores) that each fetched every game independently |
| B | `accumulate_from_stats_tab.py` | `{state}_all_stats_tab_{sport}_{season}.json` | Unchanged script; runs alongside stage A instead of waiting for it, since it only ever needed the team list |
| C | `Accumulation_data.py`, `merge_all_stats_tab.py`, `fix_total_games_checked.py` | `Final_scraped_data/Final_{state}_accumulated_{sport}_{ss}.json` | Unchanged — reads stage A + B's output exactly as before |

Downstream consumers should read only the final `Final_*` file, same as
always.

The **old 4-stage sequential pipeline** (`APP/pipeline.py`, `app.py`,
`scrape_box_scores.py`'s standalone path) is still present for reference
and comparison — it fetches every game's page twice (once to classify it,
once to store it), which is the inefficiency `APP_v2` fixes. See
`APP_v2/combined_scraper.py`'s own docstring for the full before/after.

### Run it

```bash
python APP_v2/run_pipeline_v2.py --state CO --sport boys --season 2025-2026
```

Useful flags: `--workers N`, `--output-dir DIR`, `--level varsity|jv|freshman`.

### Smoke-test before a full state

```bash
python APP_v2/run_pipeline_v2.py --state CO --sport boys --season 2025-2026 \
  --limit 5 --output-dir test_output
```

`--limit N` processes only the first N teams in both the combined fetch
and the stats-tab stage — worth doing before committing to a full state.

To verify the fetch path itself at any time:

```bash
python verify_boxscore_live.py
```

### Streamlit UI

```bash
streamlit run streamlit_app.py
```

Wraps the pipeline as a subprocess and renders live progress.

## Resuming

Every stage records processed teams in its output file and skips them on a
re-run. **This also means a re-run after a parser fix will do nothing** — the
old file still lists every team as processed. Move or delete the affected
state's output files first to force a genuine re-scrape.

## Team master lists

`boys_basketball_all_states*.json` / `girls_basketball_all_states*.json` are
tracked because the scraper needs them at runtime — to enumerate a state's
teams and to resolve opponents to canonical names/ids. The `_25-26` variants
are preferred when present; the un-suffixed files are the fallback.
Regenerate with `state_teams_counter.py`.

Scraped output is **not** tracked (see `.gitignore`) — a full set runs to
several GB and single files can exceed GitHub's 100 MB limit.

## Notes on MaxPreps' 2026 redesign

Game pages became tabbed (Recap/Stats/Roster/Matchup) React Server Components:

* Player stats render only under `?tab=stats`; the default Recap view has none.
* The HTML shows only **one** team (a client-side switcher picks it), so stats
  are read from the page's embedded Next.js RSC payload instead — it carries
  **both** teams, and each player's athlete link identifies their school, which
  makes team attribution deterministic.
* Game URLs are now `/{state}/{sport}[/{gender}]/game/{a}-vs-{b}/{date}/`.

MaxPreps' own data is often partial — listed players' points can sum to less
than the team total they publish. That's upstream, not a parsing error.
