## This invocation: a TRIGGERED tick (runner: Railway supervisor)

The runner ran `python3 core/watch.py check` itself just before starting this
session, and it reported `"trigger": true`. Treat that verdict exactly as if
you had run the check in this session (CYCLE.md, "Tick types", TRIGGERED):

- Do not run `core/watch.py check` again. The fire is already recorded in
  `journal/watch-state.json` and `journal/watch-triggers.jsonl`; commit those
  files with this cycle, which is how they reach origin.
- Skip step 0b's pacing and step 4's broad scan. The candidate set is the
  verdict's `context` array below (plus at most one narrow gamma fetch when a
  trigger is a new market). Every other step runs as usual, settlement duties
  included. Before any bet, check the ledger for an open position on the same
  market: a duplicate means the watcher double-fired; log it, do not re-bet.
- `PHIL_LEASE=exempt-triggered`: the runner skipped the lease, as CYCLE.md
  prescribes for TRIGGERED ticks.
- Open the cycle log detail with `(TRIGGERED cycle: <key>` and commit as
  `cycle(triggered): <UTCdate-HHMM> <key> placed M settled N`, where `<key>`
  is the first key below.

Watch verdict:

```json
{verdict}
```
