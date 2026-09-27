#!/usr/bin/env python3
"""Start a fresh paper run: archive the journal, reset position-bound state.

OPERATOR TOOL. deploy/ is operator-owned; the trading agent may not edit it.

Why: a fork inherits the upstream run's journal, so core/score.py would keep
grading someone else's bets. A fresh run gives this operator a scoreboard that
starts at the protected bankroll and measures only what this agent does next.

What moves to archive/<label>/ (git mv, so history and blame survive):
  - everything in journal/ except the operator decision records
    (journal/*-decision.md), which document why core/ behaves as it does
  - strategy/funnel.jsonl, the research-funnel log of the archived run

What resets in place:
  - journal/: empty ledger.jsonl, forecasts.jsonl and cycles.log, an empty
    retros/, a proposals.md that keeps the old header, and an
    operator-notes.md whose first entry tells the agent what happened
  - strategy/schedule.json: pacing hold cleared, watch_items emptied
  - strategy/watchlist.json: price_moves and calendar emptied
    (both held obligations keyed to bets and forecasts that are now archived)
  - config/run.json: run metadata; the dashboard treats the latest commit
    touching it as the start of the run

What stays: every lesson. strategy/playbook.md, risk.json, tools/,
discovery.py and the screener config are the point of inheriting a trained
agent; a fresh scoreboard judges them from zero.

Usage:
  python3 deploy/fresh_start.py [--label NAME] [--runner NAME] [--commit]
"""
import argparse
import datetime as dt
import json
import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "journal"
KEEP_IN_JOURNAL = {"lane-coverage-decision.md", "screener-rank-decision.md",
                   "screener-value-decision.md"}
TARGET_SETTLED_BETS = 100
COMPARISON_WINDOW = 20


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, check=check)


def write_json(path, obj):
    # Same shape the agent writes (indent 2, literal unicode), so resets
    # diff as the fields that changed and nothing else.
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def final_scoreboard():
    """The outgoing run's headline numbers, for the archive README."""
    out = subprocess.run([sys.executable, "core/score.py", "--json", "--skip-mtm"],
                         cwd=ROOT, capture_output=True, text=True)
    try:
        report = json.loads(out.stdout)
    except json.JSONDecodeError:
        return None
    overall = report.get("overall")
    forecasts = (report.get("forecasts") or {}).get("overall")
    return {"bets": overall, "forecasts": forecasts}


def move(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    rel = str(src.relative_to(ROOT))
    if git("ls-files", "--error-unmatch", rel, check=False).returncode == 0 or src.is_dir():
        moved = git("mv", rel, str(dst.relative_to(ROOT)), check=False)
        if moved.returncode == 0:
            return
    shutil.move(str(src), str(dst))  # untracked leftovers


def header_before_first_rule(text):
    """Lines up to the first '---' rule: the file's own format preamble."""
    lines = []
    for line in text.splitlines():
        if line.strip() == "---":
            break
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def main():
    today = dt.datetime.now(dt.timezone.utc)
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--label", default=f"upstream-{today:%Y-%m-%d}",
                    help="archive/<label>/ directory name (default: upstream-<UTC date>)")
    ap.add_argument("--runner", default="railway",
                    help="runner id named in the operator note (default: railway)")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="skip the clean-working-tree check")
    ap.add_argument("--commit", action="store_true",
                    help="commit the result as an operator: commit")
    args = ap.parse_args()

    if not args.allow_dirty and git("status", "--porcelain").stdout.strip():
        sys.exit("fresh_start: working tree is dirty; commit or stash first (or --allow-dirty)")
    dest = ROOT / "archive" / args.label
    if dest.exists():
        sys.exit(f"fresh_start: {dest.relative_to(ROOT)} already exists; pick another --label")

    stamp = today.strftime("%Y-%m-%dT%H:%M:%SZ")
    source = git("rev-parse", "--short", "HEAD").stdout.strip()
    protected = json.loads((ROOT / "config" / "protected.json").read_text())
    bankroll = protected["sim_bankroll_usd"]
    board = final_scoreboard()
    old_proposals = (JOURNAL / "proposals.md").read_text() if (JOURNAL / "proposals.md").exists() else ""
    old_notes = (JOURNAL / "operator-notes.md").read_text() if (JOURNAL / "operator-notes.md").exists() else ""

    # 1. Archive the journal (all but the operator decision records) and the funnel.
    moved = []
    for entry in sorted(JOURNAL.iterdir()):
        if entry.name in KEEP_IN_JOURNAL:
            continue
        move(entry, dest / "journal" / entry.name)
        moved.append(f"journal/{entry.name}")
    funnel = ROOT / "strategy" / "funnel.jsonl"
    if funnel.exists():
        move(funnel, dest / "strategy" / "funnel.jsonl")
        moved.append("strategy/funnel.jsonl")

    # 2. Fresh journal. Empty files rather than absent ones: loop.sh's pacing
    # check opens cycles.log directly, and CI's forward-test reads forecasts.
    for name in ("ledger.jsonl", "forecasts.jsonl", "cycles.log"):
        (JOURNAL / name).write_text("")
    (JOURNAL / "retros").mkdir(exist_ok=True)
    (JOURNAL / "retros" / ".gitkeep").write_text("")
    funnel.write_text("")

    proposals_head = header_before_first_rule(old_proposals) if old_proposals else "# Proposals to the operator\n"
    (JOURNAL / "proposals.md").write_text(
        proposals_head
        + f"\nProposals from the archived run live in `archive/{args.label}/journal/proposals.md`.\n"
        + "\n---\n")

    notes_head = header_before_first_rule(old_notes).split("\n## ")[0].rstrip() + "\n" \
        if old_notes else "# Operator notes\n"
    (JOURNAL / "operator-notes.md").write_text(notes_head + f"""
## {stamp[:10]} {stamp[11:16]}Z — new operator, fresh paper run, one runner (operator)

This checkout is a fork with a new operator. What changed, and what did not:

- **Fresh scoreboard.** The previous run's journal (ledger, forecasts,
  retros, cycle log, screener history, notes, proposals) moved to
  `archive/{args.label}/` at `{source}`. The paper bankroll restarts at
  ${bankroll:,.2f} and `core/score.py` now grades only this run.
- **Your strategy did not reset.** `strategy/playbook.md`, `risk.json`,
  `tools/`, `discovery.py` and the screener config carry every lesson from
  the archived run. Retros the playbook cites by name now live under
  `archive/{args.label}/journal/retros/`; read them there, never edit them.
- **Stale state was cleared.** `strategy/schedule.json` watch_items and
  `strategy/watchlist.json` price_moves/calendar referenced archived bets
  and forecasts by id, so they are empty (the pacing hold is cleared too).
  Rebuild them from your own research as this run's positions open.
- **One runner.** Hourly ticks, the ~15-minute watch check (TRIGGERED
  cycles) and the daily deep retro all come from a single `{args.runner}`
  container (`deploy/supervisor.py`). There is no second runner, no Pearl
  Connect signer and no mech marketplace, so CYCLE.md step 5a never applies.
- **Paper only.** `real_trading_enabled` is false in `config/protected.json`.
- **The goal of this run** is {TARGET_SETTLED_BETS} settled paper bets, judged on
  brier_delta rather than P&L: is your probability a better forecast than the
  price you paid? Keep recording a forecast for everything you research to a
  concrete number; that stream is where calibration is learned fastest.
""")

    # 3. Position-bound state in strategy/: obligations keyed to archived ids.
    schedule_path = ROOT / "strategy" / "schedule.json"
    schedule = json.loads(schedule_path.read_text(encoding="utf-8"))
    schedule["next_full_cycle_after"] = None
    schedule["reason"] = (f"RESET {stamp} by the operator: fresh paper run, pacing hold cleared "
                          f"(journal/operator-notes.md).")
    schedule["watch_items"] = []
    write_json(schedule_path, schedule)

    watchlist_path = ROOT / "strategy" / "watchlist.json"
    watchlist = json.loads(watchlist_path.read_text(encoding="utf-8"))
    watchlist["price_moves"] = []
    watchlist["calendar"] = []
    write_json(watchlist_path, watchlist)

    # 4. Run metadata, operator-owned (config/ is protected).
    write_json(ROOT / "config" / "run.json", {
        "_comment": "PROTECTED run metadata, written by deploy/fresh_start.py. The dashboard "
                    "treats the latest commit touching this file as the start of the run.",
        "label": f"fresh-{stamp[:10]}",
        "started_utc": stamp,
        "starting_bankroll_usd": bankroll,
        "target_settled_bets": TARGET_SETTLED_BETS,
        "comparison_window": COMPARISON_WINDOW,
        "archived_run": f"archive/{args.label}/",
        "source_commit": source,
    })

    # 5. A README for the archive, with the outgoing run's final numbers.
    lines = [f"# Archived run: {args.label}", "",
             f"Archived {stamp} from commit `{source}` by `deploy/fresh_start.py`.",
             "Read-only history: the agent may cite these files, never edit them.", ""]
    if board and board.get("bets"):
        b = board["bets"]
        lines += ["## Final scoreboard (settled paper bets)", "",
                  "| settled | wins | win rate | P&L | ROI | brier agent | brier market | brier_delta |",
                  "|---|---|---|---|---|---|---|---|",
                  f"| {b['n']} | {b['wins']} | {b['win_rate']:.3f} | ${b['pnl_usd']:+,.2f} | {b['roi']:+.3f} "
                  f"| {b['brier_agent']:.4f} | {b['brier_market']:.4f} | {b['brier_delta']:+.4f} |", ""]
    if board and board.get("forecasts"):
        f = board["forecasts"]
        lines += [f"Stake-free forecasts: {f['n']} settled, brier_delta {f['brier_delta']:+.4f} "
                  "(mid baseline).", ""]
    lines += ["## Contents", ""] + [f"- `{m}`" for m in moved] + [""]
    (dest / "README.md").write_text("\n".join(lines))

    print(json.dumps({"archived_to": str(dest.relative_to(ROOT)), "moved": len(moved),
                      "source_commit": source, "started_utc": stamp,
                      "bankroll_usd": bankroll}, indent=2))

    if args.commit:
        git("add", "-A")
        git("commit", "-q", "-m", f"operator: fresh paper run at ${bankroll:,.0f} "
            f"(journal archived to archive/{args.label}/)")
        print(git("log", "--oneline", "-1").stdout.strip())


if __name__ == "__main__":
    main()
