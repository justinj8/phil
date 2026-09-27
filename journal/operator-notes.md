# Operator notes

Observations from the human operator's side that the cycle agents cannot
reconstruct after the fact. Treat as evidence in retros.

## 2026-09-27 03:47Z — new operator, fresh paper run, one runner (operator)

This checkout is a fork with a new operator. What changed, and what did not:

- **Fresh scoreboard.** The previous run's journal (ledger, forecasts,
  retros, cycle log, screener history, notes, proposals) moved to
  `archive/upstream-2026-09-27/` at `6c2b6a4`. The paper bankroll restarts at
  $1,000.00 and `core/score.py` now grades only this run.
- **Your strategy did not reset.** `strategy/playbook.md`, `risk.json`,
  `tools/`, `discovery.py` and the screener config carry every lesson from
  the archived run. Retros the playbook cites by name now live under
  `archive/upstream-2026-09-27/journal/retros/`; read them there, never edit them.
- **Stale state was cleared.** `strategy/schedule.json` watch_items and
  `strategy/watchlist.json` price_moves/calendar referenced archived bets
  and forecasts by id, so they are empty (the pacing hold is cleared too).
  Rebuild them from your own research as this run's positions open.
- **One runner.** Hourly ticks, the ~15-minute watch check (TRIGGERED
  cycles) and the daily deep retro all come from a single `railway`
  container (`deploy/supervisor.py`). There is no second runner, no Pearl
  Connect signer and no mech marketplace, so CYCLE.md step 5a never applies.
- **Paper only.** `real_trading_enabled` is false in `config/protected.json`.
- **The goal of this run** is 100 settled paper bets, judged on
  brier_delta rather than P&L: is your probability a better forecast than the
  price you paid? Keep recording a forecast for everything you research to a
  concrete number; that stream is where calibration is learned fastest.
