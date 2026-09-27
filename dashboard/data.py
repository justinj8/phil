"""Read Phil's run into one JSON snapshot for the dashboard. Stdlib only.

Everything here is READ-ONLY: the journal, the configs, git history and the
supervisor's status file. Scoring goes through the run's own core/score.py
(imported from PHIL_HOME, re-imported when it changes) so the dashboard can
never disagree with the scoreboard Phil grades itself against.

Environment:
  PHIL_HOME     the Phil checkout to read (default: this repo)
  PHIL_RUNTIME  the supervisor's runtime dir, holding status.json and logs/
                (default: none - the runner panel then says "not connected")
"""
import datetime as dt
import importlib.util
import json
import math
import os
import pathlib
import re
import subprocess
import sys
import threading
from collections import defaultdict

import verdict

HOME = pathlib.Path(os.environ.get("PHIL_HOME") or pathlib.Path(__file__).resolve().parent.parent)
RUNTIME = pathlib.Path(os.environ["PHIL_RUNTIME"]) if os.environ.get("PHIL_RUNTIME") else None

DEFAULT_TARGET = 100   # settled bets that end the experiment (config/run.json overrides)
DEFAULT_WINDOW = 20    # bets per end of the first-vs-last comparison
STATUS_STALE_S = 300   # a supervisor that has not written status for this long is suspect

# How a commit's strategy/ edits are classified. "Lessons" are the files that
# encode judgment; pacing/watch edits are operational and counted apart, so a
# busy watchlist cannot inflate "strategy changes".
STRATEGY_KINDS = [
    ("judgment", re.compile(r"^strategy/(playbook\.md|risk\.json|policy\.py)$")),
    ("tools", re.compile(r"^strategy/tools/")),
    ("sensing", re.compile(r"^strategy/(discovery\.py|screener-[^/]+)$")),
    ("pacing", re.compile(r"^strategy/(schedule\.json|watchlist\.json)$")),
]
LESSON_KINDS = {"judgment", "tools", "sensing"}


# --- small readers ------------------------------------------------------------

def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip().replace(" ", "T").replace("Z", "+00:00")
    if s[-3:] in ("+00", "-00"):
        s += ":00"
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def load_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_jsonl(path):
    """Rows of a JSONL file. A torn last line (a writer mid-append) is skipped, not fatal."""
    rows, bad = [], 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows, bad
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            bad += 1
    return rows, bad


def git(*args, timeout=20):
    """stdout of a read-only git command in PHIL_HOME, or None on any failure.

    safe.directory=*: on Railway the dashboard runs as its own unprivileged
    user, reading a checkout another user owns.
    """
    try:
        out = subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=HOME,
                             capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


# --- the run's own scoring engine --------------------------------------------

class _Engine:
    """core/score.py from PHIL_HOME, re-imported whenever the file changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._mtime = None
        self._module = None
        self.error = None

    def get(self):
        path = HOME / "core" / "score.py"
        with self._lock:
            try:
                mtime = path.stat().st_mtime
            except OSError as e:
                self.error = f"core/score.py unreadable: {e}"
                return self._module
            if self._module is None or mtime != self._mtime:
                core = str(path.parent)
                if core not in sys.path:
                    sys.path.insert(0, core)   # score.py imports pmapi from beside itself
                try:
                    spec = importlib.util.spec_from_file_location("phil_score", path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    self._module, self._mtime, self.error = module, mtime, None
                except Exception as e:  # noqa: BLE001 - surfaced on the page, never fatal
                    self.error = f"core/score.py failed to load: {type(e).__name__}: {e}"
            return self._module


ENGINE = _Engine()


# --- bets --------------------------------------------------------------------

def outcome(e):
    return 1.0 if e["status"] == "won" else 0.0


def trade_delta(e):
    """This bet's contribution to brier_delta: (est - y)^2 - (market - y)^2.

    Negative means Phil's probability was the better forecast of the two.
    """
    y = outcome(e)
    return (e["est_prob"] - y) ** 2 - (e["market_prob_at_entry"] - y) ** 2


def placed_order(rows):
    return sorted(rows, key=lambda e: (e.get("ts") or "", e.get("id") or ""))


def settled_order(rows):
    return sorted(rows, key=lambda e: (e.get("settled_ts") or e.get("ts") or "", e.get("ts") or ""))


def stats(score, rows):
    """score.py's own stats() plus the few fields the dashboard adds."""
    if not rows:
        return None
    s = dict(score.stats(rows))
    s["losses"] = s["n"] - s["wins"]
    s["staked_usd"] = round(sum(e["stake_usd"] for e in rows), 2)
    edges = [e["edge"] for e in rows if isinstance(e.get("edge"), (int, float))]
    s["avg_edge"] = round(sum(edges) / len(edges), 4) if edges else None
    return s


def window(score, rows, commits):
    """One end of the first-vs-last comparison: stats plus the lesson commits
    that landed while these bets were being placed."""
    s = stats(score, rows)
    start, end = parse_iso(rows[0].get("ts")), parse_iso(rows[-1].get("ts"))
    inside = [c for c in commits if c["lesson"] and start and end
              and start.timestamp() <= c["ct"] <= end.timestamp()]
    s.update({
        "placed_from": rows[0].get("ts"), "placed_to": rows[-1].get("ts"),
        "per_trade_delta": [round(trade_delta(e), 6) for e in rows],
        "strategy_changes": len(inside),
        "strategy_commits": [{"short": c["short"], "subject": c["subject"]} for c in inside[:6]],
    })
    return s


def comparison(score, settled, commits, size):
    """First N vs last N settled bets, in the order they were PLACED - a bet
    reflects the strategy in force when it was placed, not when it resolved."""
    ordered = placed_order(settled)
    out = {"window": size, "settled": len(ordered), "needed": 2 * size,
           "first": None, "last": None, "verdict": None}
    if ordered:
        out["first"] = window(score, ordered[:size], commits)
    if len(ordered) < 2 * size:
        out["verdict"] = {"verdict": "collecting",
                          "reason": f"The two windows stop overlapping at {2 * size} settled bets "
                                    f"({len(ordered)} so far)."}
        return out
    out["last"] = window(score, ordered[-size:], commits)
    try:
        out["verdict"] = verdict.judge_improvement(out["first"], out["last"])
    except NotImplementedError:
        out["verdict"] = {"verdict": "unjudged",
                          "reason": "No verdict rule yet: implement judge_improvement() "
                                    "in dashboard/verdict.py."}
    except Exception as e:  # noqa: BLE001 - a bad rule must not take the page down
        out["verdict"] = {"verdict": "error", "reason": f"verdict.py raised {type(e).__name__}: {e}"}
    return out


def pnl_series(settled, start_utc):
    """Cumulative realized P&L in settlement order - when money became real."""
    points = [{"t": start_utc, "pnl": 0.0, "id": None}] if start_utc else []
    total = 0.0
    for e in settled_order(settled):
        total += e.get("pnl_usd") or 0.0
        points.append({"t": e.get("settled_ts") or e.get("ts"), "pnl": round(total, 2),
                       "id": e["id"], "q": (e.get("question") or "")[:90],
                       "status": e["status"], "delta_usd": round(e.get("pnl_usd") or 0.0, 2)})
    return points


def skill_series(settled, size):
    """Rolling brier_delta over the last `size` bets, in placement order."""
    out, deltas = [], []
    for i, e in enumerate(placed_order(settled), 1):
        deltas.append(trade_delta(e))
        tail = deltas[-size:]
        out.append({"n": i, "rolling": round(sum(tail) / len(tail), 4),
                    "warming": i < size, "delta": round(deltas[-1], 4),
                    "t": e.get("ts"), "id": e["id"], "q": (e.get("question") or "")[:90],
                    "status": e["status"]})
    return out


def calibration(rows, prob_key):
    """Reliability buckets as score.py cuts them (tenths), placed at their mean estimate."""
    buckets = defaultdict(list)
    for r in rows:
        buckets[min(int(r[prob_key] * 10), 9)].append(r)
    out = []
    for b in sorted(buckets):
        rs = buckets[b]
        out.append({"lo": b / 10, "hi": (b + 1) / 10, "n": len(rs),
                    "mean_est": round(sum(r[prob_key] for r in rs) / len(rs), 4),
                    "realized": round(sum(1 for r in rs if r["status"] == "won") / len(rs), 4)})
    return out


def breakdown(score, settled, key):
    groups = defaultdict(list)
    for e in settled:
        groups[e.get(key) or "unclassified"].append(e)
    rows = [dict(stats(score, es), name=name) for name, es in groups.items()]
    return sorted(rows, key=lambda r: (-r["n"], r["name"]))


def trade_row(e):
    return {k: e.get(k) for k in (
        "id", "ts", "market_id", "question", "slug", "end_date", "outcome", "entry_price",
        "est_prob", "edge", "stake_usd", "shares", "category", "edge_class", "status",
        "settled_ts", "pnl_usd", "outcome_won", "strategy_rev")} | {
        "rationale": (e.get("rationale") or "")[:600]}


# --- git: what Phil changed about itself --------------------------------------

def run_start_commit():
    """The latest commit touching config/run.json marks the start of the run."""
    sha = (git("log", "-1", "--format=%H", "--", "config/run.json") or "").strip()
    return sha or None


def classify(files):
    kinds = set()
    for f in files:
        for kind, rx in STRATEGY_KINDS:
            if rx.search(f):
                kinds.add(kind)
                break
    return sorted(kinds)


def commits_since(start):
    rng = [f"{start}..HEAD"] if start else ["-n", "200"]
    log = git("log", "--no-merges", "-n", "500", "--format=%H%x1f%h%x1f%ct%x1f%s", *rng)
    if log is None:
        return None
    files = defaultdict(list)
    touched = git("log", "--no-merges", "-n", "500", "--format=%x1e%H", "--name-only", *rng,
                  "--", "strategy/", ":(exclude)strategy/funnel.jsonl") or ""
    for block in touched.split("\x1e"):
        lines = [x for x in block.strip().splitlines() if x.strip()]
        if lines:
            files[lines[0]] = lines[1:]
    out = []
    for line in log.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, short, ct, subject = parts
        kind = ("deep-retro" if subject.startswith("deep-retro") else
                "retro" if subject.startswith("retro") else
                "triggered" if subject.startswith("cycle(triggered)") else
                "cycle" if subject.startswith("cycle") else
                "operator" if subject.startswith("operator:") else "other")
        kinds = classify(files.get(sha, []))
        out.append({"sha": sha, "short": short, "ct": int(ct), "t": iso(dt.datetime.fromtimestamp(int(ct), dt.timezone.utc)),
                    "subject": subject, "kind": kind, "files": files.get(sha, [])[:12],
                    "strategy_kinds": kinds,
                    "lesson": kind != "operator" and bool(LESSON_KINDS & set(kinds))})
    return out


def repo_info():
    head = (git("rev-parse", "HEAD") or "").strip() or None
    branch = (git("rev-parse", "--abbrev-ref", "HEAD") or "").strip() or None
    origin = (git("remote", "get-url", "origin") or "").strip() or None
    web = None
    if origin:
        m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$", origin)
        if m:
            web = f"https://github.com/{m.group(1)}/{m.group(2)}"
    ahead = behind = None
    counts = git("rev-list", "--left-right", "--count", "origin/main...HEAD")
    if counts:
        try:
            behind, ahead = (int(x) for x in counts.split())
        except ValueError:
            pass
    last = git("log", "-1", "--format=%ct%x1f%s")
    last_commit = None
    if last and "\x1f" in last:
        ct, subject = last.strip().split("\x1f", 1)
        last_commit = {"t": iso(dt.datetime.fromtimestamp(int(ct), dt.timezone.utc)), "subject": subject}
    return {"head": head, "branch": branch, "web": web, "unpushed": ahead, "behind": behind,
            "last_commit": last_commit}


# --- journal text -------------------------------------------------------------

CYCLE_LINE = re.compile(r"^(\S+Z) cycle done: (.*)$")


def cycles(limit=40):
    try:
        lines = (HOME / "journal" / "cycles.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"recent": [], "last_24h": {}}
    now = utcnow()
    recent, last_24h = [], defaultdict(int)
    for line in lines:
        m = CYCLE_LINE.match(line.strip())
        if not m:
            continue
        ts, rest = m.groups()
        tick = re.search(r"\((FULL|LIGHT|TRIGGERED)", rest)
        kind = tick.group(1) if tick else "?"
        when = parse_iso(ts)
        if when and (now - when).total_seconds() <= 86400:
            last_24h[kind] += 1
        placed = re.search(r"placed (\d+)", rest)
        settled = re.search(r"settled (\d+)", rest)
        cash = re.search(r"cash \$(\d[\d,]*(?:\.\d+)?)", rest)   # the agent may end on "$950.00."
        recent.append({"t": ts, "tick": kind,
                       "placed": int(placed.group(1)) if placed else None,
                       "settled": int(settled.group(1)) if settled else None,
                       "cash": float(cash.group(1).replace(",", "")) if cash else None,
                       "text": rest[:400]})
    return {"recent": recent[-limit:][::-1], "last_24h": dict(last_24h), "total": len(recent)}


RETRO_NAME = re.compile(r"^(RETRO|DEEP)-[A-Za-z0-9_.-]+\.md$")


def retros(limit=60):
    folder = HOME / "journal" / "retros"
    try:
        names = [p.name for p in folder.iterdir() if RETRO_NAME.match(p.name)]
    except OSError:
        return []

    def when(name):
        digits = re.sub(r"\D", "", name)
        return (digits + "0000")[:12]

    out = []
    for name in sorted(names, key=when, reverse=True)[:limit]:
        try:
            text = (folder / name).read_text(encoding="utf-8")
        except OSError:
            continue
        title = next((ln.lstrip("# ").strip() for ln in text.splitlines() if ln.startswith("#")), name)
        body = " ".join(ln.strip() for ln in text.splitlines()
                        if ln.strip() and not ln.startswith("#") and not ln.startswith("|"))
        out.append({"name": name, "kind": "deep" if name.startswith("DEEP") else "retro",
                    "when": when(name), "title": title[:160], "summary": body[:320]})
    return out


def read_retro(name):
    if not RETRO_NAME.match(name or ""):
        return None
    path = HOME / "journal" / "retros" / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


# --- the supervisor -------------------------------------------------------------

LOG_NAME = re.compile(r"^[0-9]{8}T[0-9]{4,6}Z-[a-z-]+\.log$")


def runner():
    if RUNTIME is None:
        return {"connected": False, "reason": "PHIL_RUNTIME not set (local preview)"}
    status = load_json(RUNTIME / "status.json")
    if status is None:
        return {"connected": False, "reason": "no status.json yet - supervisor starting?"}
    updated = parse_iso(status.get("updated_utc"))
    status["connected"] = True
    status["stale"] = updated is None or (utcnow() - updated).total_seconds() > STATUS_STALE_S
    return status


def read_log(name, lines=400):
    if RUNTIME is None or not LOG_NAME.match(name or ""):
        return None
    path = RUNTIME / "logs" / name
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 512 * 1024))   # the tail is what matters
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    return "\n".join(text.splitlines()[-lines:])


# --- live marks -------------------------------------------------------------------

def mark_open_positions():
    """Live CLOB marks for open bets, via the run's own score.mark_to_market."""
    score = ENGINE.get()
    if score is None:
        return {"error": ENGINE.error, "rows": []}
    ledger, _ = load_jsonl(HOME / "journal" / "ledger.jsonl")
    open_rows = [e for e in ledger if e.get("status") == "open"]
    if not open_rows:
        return {"rows": []}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=6) as pool:
        marked = list(pool.map(lambda e: score.mark_to_market([e])[0], open_rows))
    return {"rows": marked}


# --- the snapshot ---------------------------------------------------------------------

def snapshot():
    now = utcnow()
    score = ENGINE.get()
    protected = load_json(HOME / "config" / "protected.json", {}) or {}
    run = load_json(HOME / "config" / "run.json", {}) or {}
    bankroll = float(run.get("starting_bankroll_usd") or protected.get("sim_bankroll_usd") or 1000.0)
    target = int(run.get("target_settled_bets") or DEFAULT_TARGET)
    size = int(run.get("comparison_window") or DEFAULT_WINDOW)

    ledger, ledger_bad = load_jsonl(HOME / "journal" / "ledger.jsonl")
    forecasts, forecasts_bad = load_jsonl(HOME / "journal" / "forecasts.jsonl")
    settled = [e for e in ledger if e.get("status") in ("won", "lost")]
    open_rows = [e for e in ledger if e.get("status") == "open"]
    voided = [e for e in ledger if e.get("status") == "void"]

    start_sha = run_start_commit() if run else None
    commits = commits_since(start_sha)
    agent_commits = [c for c in (commits or []) if c["kind"] != "operator"]

    realized = round(sum(e.get("pnl_usd") or 0.0 for e in settled), 2)
    open_cost = round(sum(e["stake_usd"] for e in open_rows), 2)
    snap = {
        "generated_utc": iso(now),
        "engine_error": ENGINE.error,
        "run": {"label": run.get("label"), "started_utc": run.get("started_utc"),
                "start_commit": start_sha, "archived_run": run.get("archived_run"),
                "target": target, "window": size, "bankroll_start": bankroll},
        "caps": {k: protected.get(k) for k in ("max_stake_usd", "max_open_positions",
                                               "max_new_positions_per_cycle",
                                               "min_entry_price", "max_entry_price",
                                               "real_trading_enabled")},
        "money": {"balance": round(bankroll + realized, 2), "realized_pnl": realized,
                  "cash": round(bankroll + realized - open_cost, 2), "open_cost": open_cost,
                  "open_count": len(open_rows), "voided": len(voided)},
        "data_warnings": [w for w in (
            f"{ledger_bad} unreadable ledger line(s)" if ledger_bad else None,
            f"{forecasts_bad} unreadable forecast line(s)" if forecasts_bad else None) if w],
        "repo": repo_info(),
        "runner": runner(),
        "cycles": cycles(),
        "retros": retros(),
    }

    lessons = [c for c in agent_commits if c["lesson"]]
    snap["lessons"] = {
        "strategy_changes": len(lessons),
        "retro_commits": sum(1 for c in agent_commits if c["kind"] in ("retro", "deep-retro")),
        "cycle_commits": sum(1 for c in agent_commits if c["kind"] in ("cycle", "triggered")),
        "pacing_edits": sum(1 for c in agent_commits if "pacing" in c["strategy_kinds"]),
        # Retros carry the lesson in their subject; other commits qualify only
        # by touching judgment, tools or sensing (pacing-only ticks are noise here).
        "commits": [c for c in (commits or [])
                    if c["kind"] in ("retro", "deep-retro", "operator") or c["lesson"]][:120],
        "git_ok": commits is not None,
    }

    if score is None:
        snap["bets"] = snap["forecasts"] = None
        return snap

    overall = stats(score, settled)
    luck = score.luck_adjusted(settled) if settled else None
    snap["bets"] = {
        "overall": overall, "luck": luck,
        "open": [trade_row(e) for e in sorted(open_rows, key=lambda e: e.get("end_date") or "")],
        "trades": [trade_row(e) for e in sorted(ledger, key=lambda e: e.get("ts") or "", reverse=True)],
        "pnl_series": pnl_series(settled, run.get("started_utc")),
        "skill_series": skill_series(settled, size),
        "calibration": calibration(settled, "est_prob"),
        "by_category": breakdown(score, settled, "category"),
        "by_edge_class": breakdown(score, settled, "edge_class"),
        "comparison": comparison(score, settled, commits or [], size),
    }

    live = [r for r in forecasts if not r.get("superseded_by")]
    fsettled = [r for r in live if r.get("status") in ("won", "lost")]
    report = score.forecast_report(forecasts)
    snap["forecasts"] = {
        "settled": len(fsettled), "open": sum(1 for r in live if r.get("status") == "open"),
        "overall": report.get("overall"), "luck": report.get("luck_adjusted"),
        "by_skip_reason": [dict(v, name=k) for k, v in (report.get("by_skip_reason") or {}).items()],
        "calibration": calibration(fsettled, "est_prob"),
    }

    # The guide's scorecard: where the run started, where it is, and the
    # snapshot taken the moment the target-th bet settled.
    at_target = None
    if len(settled) >= target:
        first_n = settled_order(settled)[:target]
        cut = parse_iso(first_n[-1].get("settled_ts"))
        s = stats(score, first_n)
        at_target = {"balance": round(bankroll + s["pnl_usd"], 2), "settled": s["n"],
                     "wins": s["wins"], "losses": s["losses"], "pnl_usd": s["pnl_usd"],
                     "brier_delta": s["brier_delta"], "reached_utc": first_n[-1].get("settled_ts"),
                     "strategy_changes": sum(1 for c in lessons if cut and c["ct"] <= cut.timestamp())}
    snap["scorecard"] = {
        "start": {"balance": bankroll, "settled": 0, "wins": 0, "losses": 0, "pnl_usd": 0.0,
                  "brier_delta": None, "strategy_changes": 0},
        "now": {"balance": round(bankroll + realized, 2), "settled": len(settled),
                "wins": overall["wins"] if overall else 0,
                "losses": overall["losses"] if overall else 0,
                "pnl_usd": realized, "brier_delta": overall["brier_delta"] if overall else None,
                "strategy_changes": len(lessons)},
        "at_target": at_target,
    }
    return snap


def finite(obj):
    """JSON-safe copy: NaN/inf become null (json.dumps would emit invalid JSON)."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [finite(v) for v in obj]
    return obj
