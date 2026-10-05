import re
import os
import sys
import json
import time
import signal
import subprocess
import streamlit as st

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

STATE_FILE = os.path.join(OUTPUT_DIR, ".scraper_state.json")
LOG_FILE   = os.path.join(OUTPUT_DIR, ".scraper.log")

STATE_NAMES = {
    "AL": "Alabama",       "AK": "Alaska",         "AZ": "Arizona",
    "AR": "Arkansas",      "CA": "California",     "CO": "Colorado",
    "CT": "Connecticut",   "DE": "Delaware",       "FL": "Florida",
    "GA": "Georgia",       "HI": "Hawaii",         "ID": "Idaho",
    "IL": "Illinois",      "IN": "Indiana",        "IA": "Iowa",
    "KS": "Kansas",        "KY": "Kentucky",       "LA": "Louisiana",
    "ME": "Maine",         "MD": "Maryland",       "MA": "Massachusetts",
    "MI": "Michigan",      "MN": "Minnesota",      "MS": "Mississippi",
    "MO": "Missouri",      "MT": "Montana",        "NE": "Nebraska",
    "NV": "Nevada",        "NH": "New Hampshire",  "NJ": "New Jersey",
    "NM": "New Mexico",    "NY": "New York",       "NC": "North Carolina",
    "ND": "North Dakota",  "OH": "Ohio",           "OK": "Oklahoma",
    "OR": "Oregon",        "PA": "Pennsylvania",   "RI": "Rhode Island",
    "SC": "South Carolina","SD": "South Dakota",   "TN": "Tennessee",
    "TX": "Texas",         "UT": "Utah",           "VT": "Vermont",
    "VA": "Virginia",      "WA": "Washington",     "WV": "West Virginia",
    "WI": "Wisconsin",     "WY": "Wyoming",        "DC": "District of Columbia",
}

SEASONS        = [f"{y}-{y+1}" for y in range(2029, 2019, -1)]
DEFAULT_SEASON = "2025-2026"
LEVELS         = ["varsity", "jv", "freshman"]
LEVEL_LABELS   = {"varsity": "Varsity", "jv": "JV", "freshman": "Freshman"}


def level_suffix(level):
    """Filename infix. Varsity stays '' so existing varsity outputs keep their
    names; JV/freshman get their own files."""
    return "" if (level or "varsity") == "varsity" else f"_{level}"


PHASE_LABELS = [
    "Phase 1 — Gap Finder",
    "Phase 2 — Stats Section (every team)",
    "Phase 3 — Box Scores",
    "Phase 4 — Final Accumulation",
]


def short_season(season):
    """'2025-2026' -> '25_26'."""
    parts = season.replace('_', '-').split('-')
    if len(parts) == 2 and all(len(p) == 4 and p.isdigit() for p in parts):
        return f"{parts[0][-2:]}_{parts[1][-2:]}"
    return season.replace('-', '_')


# ── Log parser ────────────────────────────────────────────────────────────────
# Maps the orchestrator's own stage banners + each child script's progress lines
# onto the four-phase UI. Banners are stable across runs; progress regexes
# match the [N/M] format every child uses.
def parse_log(line, state):
    # Stage banners from APP_v2/run_pipeline_v2.py — definitive phase markers.
    if "STAGE 1/4" in line:
        state["phase"] = 1
        state["done"]  = 0
    elif "STAGE 2/4" in line:
        state["phase"] = 2
        state["done"]  = 0
    elif "STAGE 3/4" in line:
        state["phase"] = 3
        state["done"]  = 0
    elif "STAGE 4/4" in line:
        state["phase"] = 4
        state["done"]  = 0

    # Phase 1 — gap finder
    if "Phase 1:" in line:
        state["phase"] = 1
        m = re.search(r"Fetching\s+(\d+)\s+schedules", line)
        if m:
            state["total"] = int(m.group(1))

    m = re.search(r"Schedules:\s*(\d+)/(\d+)", line)
    if m:
        state["phase"] = 1
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))

    m = re.search(r"\[\s*(\d+)/\s*(\d+)\].*Full:\s*(\d+).*Part:\s*(\d+)\s*\|\s*(.+)", line)
    if m:
        state["phase"]   = 1
        state["done"]    = int(m.group(1))
        state["total"]   = int(m.group(2))
        state["full"]    = int(m.group(3))
        state["partial"] = int(m.group(4))
        state["team"]    = m.group(5).strip()

    # Phase 2 — stats-tab (accumulate_from_stats_tab.py)
    # `[ 12/326] OK     | Team Name      GP=27  players=11`
    m = re.search(r"\[\s*(\d+)/\s*(\d+)\]\s+(?:OK|skip:|CRASH)\s+\|", line)
    if m and state.get("phase", 0) == 2:
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))

    # Phase 3 — box scores
    m = re.search(r"Processing team\s+(\d+)/(\d+):\s*(.+)", line)
    if m:
        state["phase"] = 3
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))
        state["team"]  = m.group(3).strip()

    m = re.search(r"\[DONE\] Added\s+(\d+)\s+games for", line)
    if m:
        state["games"] = state.get("games", 0) + int(m.group(1))

    # Phase 4 — final accumulation
    if "4a) per-game accumulator" in line:
        state["phase"] = 4
    m = re.search(r"Accumulating:\s*(\d+)/(\d+)", line)
    if m:
        state["phase"] = 4
        state["done"]  = int(m.group(1))
        state["total"] = int(m.group(2))

    if "PIPELINE COMPLETE" in line:
        state["phase"] = 4
        state["done"]  = state.get("total", 1) or 1

    return state


# ── Disk state helpers ────────────────────────────────────────────────────────
def load_disk_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_disk_state(d):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f)

def clear_disk_state():
    for p in [STATE_FILE, LOG_FILE]:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass

def is_pid_running(pid):
    """Reliable cross-platform PID check. Uses /proc on Linux (Streamlit Cloud)."""
    if pid is None:
        return False
    try:
        pid = int(pid)
        if os.path.exists(f"/proc/{pid}"):
            return True
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def stop_pid(pid):
    if pid is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           check=False, capture_output=True)
            return True
        except Exception:
            return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        return True
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except Exception:
            return False

def tail_log(n=60):
    if not os.path.exists(LOG_FILE):
        return ""
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "\n".join(l.rstrip() for l in lines[-n:] if l.strip())
    except Exception:
        return ""

def parse_progress_from_log():
    prog = {"phase": 1, "done": 0, "total": 0,
            "full": 0, "partial": 0, "team": "", "games": 0}
    if not os.path.exists(LOG_FILE):
        return prog
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                prog = parse_log(line.rstrip(), prog)
    except Exception:
        pass
    return prog


# ── Progress renderer ─────────────────────────────────────────────────────────
def render_progress(ph, state):
    phase = state["phase"]
    done  = state["done"]
    total = state["total"]
    pct   = done / total if total > 0 else 0.0

    with ph.container():
        # Four phase chips in one row.
        cols = st.columns(4)
        for i, (col, label) in enumerate(zip(cols, PHASE_LABELS), 1):
            if i < phase:
                col.success(f"✅ {label}")
            elif i == phase:
                col.warning(f"⏳ {label}")
            else:
                col.info(f"🔒 {label}")

        label_idx = min(max(phase, 1), len(PHASE_LABELS)) - 1
        if total > 0:
            st.progress(pct, text=f"{PHASE_LABELS[label_idx]}: **{done} / {total}** ({pct*100:.1f}%)")
        else:
            st.progress(0.0, text=f"{PHASE_LABELS[label_idx]}: starting…")

        team = state.get("team", "")
        if team:
            st.caption(f"⚙️ Currently processing: **{team}**")

        if phase >= 1 and done > 0:
            m1, m2, m3, m4, m5 = st.columns(5)
            m1.metric("Teams Done",         done)
            m2.metric("Full Box Scores",    state["full"])
            m3.metric("Partial Box Scores", state["partial"])
            m4.metric("No Box Scores",      max(0, done - state["full"] - state["partial"]))
            m5.metric("Games Scraped",      state.get("games", 0))


# ── Download helper ───────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False, max_entries=8)
def _read_file_bytes(filepath, mtime, size):
    """File contents for a download button.

    Cached on (path, mtime, size) so a rerun that changes nothing doesn't
    re-read the file — the previous version read and UTF-8-decoded every output
    file on EVERY rerun, which with a 1.5s auto-refresh meant re-reading ~13 MB
    a second for a mid-size state (far more for TX). mtime/size are arguments
    rather than looked up inside so they participate in the cache key.
    """
    with open(filepath, "rb") as f:      # bytes: no decode cost
        return f.read()


def _file_stat(filepath):
    """(exists, size_bytes, mtime) without reading the contents."""
    try:
        st_ = os.stat(filepath)
        return True, st_.st_size, st_.st_mtime
    except OSError:
        return False, 0, 0.0


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


def show_download(placeholder, filepath, label, allow_download=True):
    """Render a download button (or a status line) for one output file.

    allow_download=False renders size/time only. Downloads are suppressed while
    the auto-refresh loop is active because each rerun re-creates the widget and
    cancels an in-flight click — the usual reason the button "does nothing".
    """
    exists, size, mtime = _file_stat(filepath)
    if not exists:
        return False
    if not allow_download:
        placeholder.info(
            f"✅ Ready — {_fmt_size(size)}\n\n"
            f"_{time.strftime('%H:%M:%S', time.localtime(mtime))}_ · "
            f"pause refresh to download"
        )
        return True
    placeholder.download_button(
        label=f"✅ {label} ({_fmt_size(size)})",
        data=_read_file_bytes(filepath, mtime, size),
        file_name=os.path.basename(filepath),
        mime="application/json",
        use_container_width=True,
        key=f"dl_{os.path.basename(filepath)}",
    )
    return True


@st.cache_data(show_spinner=False, max_entries=4)
def _build_zip(paths_with_stats):
    """Zip the existing output files into memory.

    Keyed on the (path, size, mtime) tuples so it only rebuilds when an output
    actually changes. JSON compresses well (~10x), which also makes the browser
    transfer far smaller than four separate raw downloads.
    """
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path, _size, _mtime in paths_with_stats:
            zf.write(path, arcname=os.path.basename(path))
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
#  PAGE
# ══════════════════════════════════════════════════════════════════════════════

st.set_page_config(page_title="MaxPreps Basketball Scraper v2", page_icon="🏀", layout="wide")
st.title("🏀 MaxPreps Basketball Scraper — Version 2")
st.markdown("Select your options and click **Start Scraping** to begin.")

with st.expander("📋 How the scraper works (click to expand)", expanded=False):
    st.markdown("""
    The pipeline runs **4 phases** in sequence. A download button appears as soon as each file is ready.

    | Phase | What happens | Output File |
    |-------|-------------|-------------|
    | **Phase 1** — Gap Finder | Fetches every team's schedule, classifies each game's box-score availability (Full / Partial / No stats). | `{state}_data_gaps_{sport}_{season}.json` |
    | **Phase 2** — Stats Section | Hits the print-stats endpoint for **every** team the gap finder discovered. Saves any team whose coach has uploaded season totals + per-player rows. | `{state}_all_stats_tab_{sport}_{season}.json` |
    | **Phase 3** — Box Scores | Scrapes per-game player box scores from every available game page. | `{state}_box_scores_{sport}_{season}.json` |
    | **Phase 4** — Final Accumulation | Per-game accumulator runs, then stats-tab data overlays it (stats-tab wins for any team that has data). TotalGamesChecked is set to `max(gap, box_count, GP)` so `GP ≤ TGC` always holds. | `Final_{state}_accumulated_{sport}_{ss}.json` |

    > **Total runtime:** typically 5–15 min for AR/LA/NM/OK, 30–50 min for TX.
    """)

st.divider()

st.divider()

# ── Load persisted state ──────────────────────────────────────────────────────
disk    = load_disk_state()
running = disk is not None and is_pid_running(disk.get("pid"))

# ── MaxPreps reachability check ───────────────────────────────────────────────
# Worth one click before a multi-hour run: MaxPreps refuses some networks, and
# the two refusals look different.
#   403 Geo-block      → the exit IP's country isn't allowed
#   406 Not Acceptable → the exit IP is a datacenter range MaxPreps rejects
#                        (confirmed for Azure/GitHub runners, and this is the
#                        likely outcome on a hosted Streamlit deployment)
# Either way every stage returns zero rows, so surface it up front rather than
# letting the user discover it from an empty output file.
with st.expander("🌐 Check MaxPreps access (run this before a long scrape)", expanded=False):
    st.caption(
        "The scraper can only reach MaxPreps from a permitted network. If this "
        "check fails, a scrape will finish 'successfully' with **0 games** — so "
        "verify here first."
    )
    if st.button("Run access check", disabled=running):
        with st.spinner("Probing MaxPreps…"):
            try:
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from scrape_box_scores import _http_backend_label, _http_get_page

                exit_ip = "unknown"
                try:
                    import requests as _rq
                    who = _rq.get("https://ipinfo.io/json", timeout=15).json()
                    exit_ip = (f"{who.get('ip')} · {who.get('country')} "
                               f"{who.get('city')} · {who.get('org')}")
                except Exception as e:
                    exit_ip = f"(lookup failed: {e})"

                probe = ("https://www.maxpreps.com/co/basketball/game/"
                         "adams-city-commerce-city-vs-westminster/12-2-2025/"
                         "?c=dc2f2ce6-a427-4c18-93ca-839e288f67a0&tab=stats")
                status, html, _final = _http_get_page(probe, timeout=30)

                st.write(f"**Transport:** `{_http_backend_label()}`")
                st.write(f"**Exit IP:** `{exit_ip}`")

                if status == 200 and html:
                    has_payload = "self.__next_f.push" in html
                    st.success(f"Reachable — HTTP {status}, {len(html):,} bytes.")
                    if has_payload:
                        st.success("Stat payload present. Scraping should work "
                                   "from this deployment.")
                    else:
                        st.warning(
                            "Page loaded but the embedded stat payload is "
                            "missing — box scores would come back empty. The "
                            "page layout may have changed again; run "
                            "`verify_boxscore_live.py` for detail."
                        )
                elif status == 403:
                    st.error(
                        "**403 Geo-block** — this network's country is not "
                        "allowed. Use a **system-wide** VPN in a permitted "
                        "region (a browser VPN extension does not route "
                        "Python). Scraping cannot work here."
                    )
                elif status == 406:
                    st.error(
                        "**406 Not Acceptable** — MaxPreps rejects this exit "
                        "IP's range (typical for cloud/datacenter hosts; "
                        "confirmed for Azure). Not fixable with headers. Run "
                        "the scraper from a residential or VPN connection "
                        "instead of a hosted deployment."
                    )
                else:
                    st.error(f"Unexpected HTTP {status} — scraping is unlikely "
                             f"to work from here.")
            except Exception as e:
                st.error(f"Access check failed to run: {type(e).__name__}: {e}")


# ── Dropdowns — only disabled while actively running ─────────────────────────
col1, col2, col3, col4 = st.columns(4)
with col1:
    state_code = st.selectbox("State", options=list(STATE_NAMES.keys()),
                               format_func=lambda x: f"{x} — {STATE_NAMES[x]}", disabled=running)
with col2:
    sport = st.selectbox("Sport", options=["boys", "girls"],
                          format_func=lambda x: "Boys Basketball" if x == "boys" else "Girls Basketball",
                          disabled=running)
with col3:
    season = st.selectbox("Season", options=SEASONS,
                           index=SEASONS.index(DEFAULT_SEASON), disabled=running)
with col4:
    # MaxPreps serves varsity at the bare team URL and nests the other levels
    # after the gender (/basketball/girls/jv/…). Each level gets its own output
    # files, so switching level never overwrites another level's data.
    level = st.selectbox("Level", options=LEVELS,
                          format_func=lambda x: LEVEL_LABELS[x], disabled=running)

st.divider()

clear_previous = st.checkbox("🗑️ Clear previous data for this state/sport/season before starting",
                              value=False, disabled=running)

# Stage 1 still walks every team, so this bounds the box-score stage rather
# than the whole run — enough to prove data actually comes back before
# committing hours to a full state.
test_mode = st.checkbox(
    "🧪 Test mode — box scores for the first 5 teams only",
    value=False, disabled=running,
    help="Recommended for a first run on a new deployment: confirms real games "
         "are returned without waiting for the full state.",
)

# ── Start button — enabled whenever scraper is not actively running ───────────
if st.button("▶ Start Scraping", type="primary", use_container_width=True, disabled=running):
    season_fn   = season.replace("-", "_")
    ss          = short_season(season)
    state_lower = state_code.lower()
    lv          = level_suffix(level)

    clear_disk_state()

    if clear_previous:
        for fname in [
            f"{state_lower}_data_gaps_{sport}{lv}_{season_fn}.json",
            f"{state_lower}_all_stats_tab_{sport}{lv}_{season_fn}.json",
            f"{state_lower}_all_stats_tab_{sport}{lv}_{season_fn}_report.json",
            f"{state_lower}_box_scores_{sport}{lv}_{season_fn}.json",
            f"{state_lower}_accumulated_stats_{sport}{lv}_{season_fn}.json",
            f"Final_{state_lower}_accumulated_{sport}{lv}_{ss}.json",
        ]:
            fpath = os.path.join(OUTPUT_DIR, fname)
            if os.path.exists(fpath):
                os.remove(fpath)

    env = os.environ.copy()
    env["DATA_DIR"]         = OUTPUT_DIR
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"]       = "1"
    env["PYTHONUNBUFFERED"] = "1"

    log_f = open(LOG_FILE, "wb")
    # v2 engine: APP_v2/run_pipeline_v2.py. Fetches each game's box-score
    # page once (not twice, like the old APP/pipeline.py) by merging what
    # used to be separate gap-finder + box-score stages, and runs the
    # stats-tab stage concurrently instead of after. --output-dir routes
    # all outputs to OUTPUT_DIR so the UI can find them via predictable
    # filenames, same as before. Its print statements are deliberately
    # shaped to match what parse_log() below already expects, so that
    # function didn't need to change for this swap.
    cmd = [sys.executable, "-u", "APP_v2/run_pipeline_v2.py",
           "--state", state_code, "--sport", sport, "--season", season,
           "--level", level,
           "--output-dir", OUTPUT_DIR]
    if test_mode:
        cmd += ["--limit", "5"]
    process = subprocess.Popen(
        cmd,
        stdout=log_f,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    log_f.close()

    save_disk_state({
        "pid":         process.pid,
        "label":       (f"{STATE_NAMES[state_code]} | "
                        f"{'Boys' if sport=='boys' else 'Girls'} Basketball | "
                        f"{LEVEL_LABELS[level]} | {season}"
                        + (" | 🧪 TEST MODE (5 teams)" if test_mode else "")),
        "gaps_file":   os.path.join(OUTPUT_DIR, f"{state_lower}_data_gaps_{sport}{lv}_{season_fn}.json"),
        "stab_file":   os.path.join(OUTPUT_DIR, f"{state_lower}_all_stats_tab_{sport}{lv}_{season_fn}.json"),
        "box_file":    os.path.join(OUTPUT_DIR, f"{state_lower}_box_scores_{sport}{lv}_{season_fn}.json"),
        "final_file":  os.path.join(OUTPUT_DIR, f"Final_{state_lower}_accumulated_{sport}{lv}_{ss}.json"),
    })
    st.rerun()

# ── Dashboard: shown while running OR after completion ────────────────────────
if disk is not None:
    st.divider()

    if running:
        info_col, btn_col = st.columns([4, 1])
        with info_col:
            st.info(f"⏳ Scraping in progress: **{disk['label']}**")
        with btn_col:
            if st.button("🛑 Restart", type="secondary", use_container_width=True,
                         help="Stop the current scrape and pick a new state/sport/season."):
                stop_pid(disk.get("pid"))
                clear_disk_state()
                st.rerun()
    else:
        if os.path.exists(disk.get("final_file", "")):
            st.success(f"🎉 Completed: **{disk['label']}** — Download your files below, then start a new scrape above.")
        elif os.path.exists(disk.get("box_file", "")):
            st.warning(f"⚠️ Pipeline stopped after Phase 3 — partial outputs available below.")
        else:
            st.warning(f"⚠️ Stopped/failed: **{disk['label']}** — Check logs below. You can start a new scrape above.")

    # Progress
    prog    = parse_progress_from_log()
    prog_ph = st.empty()
    render_progress(prog_ph, prog)

    st.divider()

    # Output files — 4 files in one row
    st.subheader("Output Files")

    # Downloads and the auto-refresh loop are mutually exclusive: every rerun
    # rebuilds the widgets, which cancels a click that's already in progress.
    # While running, offer to pause the refresh instead of silently handing out
    # buttons that don't work.
    paused = False
    if running:
        paused = st.checkbox(
            "⏸ Pause auto-refresh to enable downloads",
            value=False,
            help="Downloads can't complete while the page refreshes every few "
                 "seconds — the rerun cancels the click. Pausing does NOT stop "
                 "the scrape; it keeps running in the background.",
        )
        if not paused:
            st.caption("↑ tick this to download files mid-run — the scrape keeps going.")
    allow_dl = (not running) or paused

    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1:
        st.markdown("**Phase 1 — Data Gaps**")
        gaps_ph = st.empty()
        if not show_download(gaps_ph, disk["gaps_file"], "Data Gaps", allow_dl):
            gaps_ph.warning("⏳ Generating...")
    with fc2:
        st.markdown("**Phase 2 — Stats Section**")
        stab_ph = st.empty()
        if not show_download(stab_ph, disk["stab_file"], "Stats Section", allow_dl):
            stab_ph.info("🔒 Waiting...")
    with fc3:
        st.markdown("**Phase 3 — Box Scores**")
        box_ph = st.empty()
        if not show_download(box_ph, disk["box_file"], "Box Scores", allow_dl):
            box_ph.info("🔒 Waiting...")
    with fc4:
        st.markdown("**Phase 4 — Final Accumulation**")
        final_ph = st.empty()
        if not show_download(final_ph, disk["final_file"], "FINAL", allow_dl):
            final_ph.info("🔒 Waiting...")

    # One-click ZIP of everything produced so far — usually ~10x smaller than
    # the raw JSON, and one click instead of four.
    if allow_dl:
        present = []
        for key in ("gaps_file", "stab_file", "box_file", "final_file"):
            path = disk.get(key)
            if not path:
                continue
            exists, size, mtime = _file_stat(path)
            if exists:
                present.append((path, size, mtime))
        if present:
            raw_total = sum(p[1] for p in present)
            zip_bytes = _build_zip(tuple(present))
            st.download_button(
                f"📦 Download ALL {len(present)} file(s) as ZIP "
                f"({_fmt_size(len(zip_bytes))}, from {_fmt_size(raw_total)} raw)",
                data=zip_bytes,
                file_name=f"{os.path.basename(disk.get('final_file', 'maxpreps_output')).replace('.json','')}_bundle.zip",
                mime="application/zip",
                use_container_width=True,
                key="dl_zip_all",
            )

    # Running locally? The files are already on disk — downloading is pointless.
    with st.expander("📁 Files on disk (no download needed if running locally)"):
        st.caption(
            "The pipeline writes straight to this folder. If you run the app on "
            "your own machine, just open it — the download buttons only matter "
            "for a hosted deployment."
        )
        st.code(OUTPUT_DIR, language=None)
        st.caption(
            "Tip: to have results land somewhere that syncs automatically, run "
            "the pipeline from a terminal with `--output-dir` pointing at a "
            "OneDrive/Google Drive/Dropbox folder:"
        )
        st.code(
            "python APP_v2/run_pipeline_v2.py --state CO --sport boys --season 2025-2026 \\\n"
            "  --output-dir \"C:/Users/you/OneDrive/maxpreps\"",
            language="bash",
        )

    # Logs
    with st.expander("📄 Logs", expanded=running):
        st.text_area("", value=tail_log(), height=300, label_visibility="collapsed")

    # After completion — show reset button
    if not running:
        st.divider()
        if st.button("🔄 Scrape Another State / Sport / Season",
                     type="primary", use_container_width=True):
            clear_disk_state()
            st.rerun()

    # Auto-refresh while running — skipped when the user pauses to download.
    # 3s rather than 1.5s: the progress numbers come from the log file, which
    # only moves every few seconds anyway, and each rerun re-renders the page.
    if running and not paused:
        time.sleep(3.0)
        st.rerun()
