# Running Phil on Railway

One Railway service runs everything: the trading loop and a live dashboard.
Paper trading only. No real money moves, and nothing in this setup can
enable it: `real_trading_enabled` is false and the supervisor never passes
`--real`.

```
Railway service (deploy/Dockerfile)
├── user phil  deploy/supervisor.py
│              ├─ every 60 min   ./loop.sh 1          hourly cycle (Claude Code, headless)
│              ├─ every 15 min   core/watch.py check  free; a fire starts a TRIGGERED cycle
│              └─ daily 04:40Z   deep retro           audits the day's strategy edits
│                     └─ commits and pushes to your fork (the diary of lessons)
├── user web   dashboard/server.py   public page, read-only, no secrets
└── volume /data
    ├── phil/      git clone of your fork: core/, strategy/, journal/
    └── runtime/   status.json, state.json, one log per session
```

The image holds only the toolchain, the supervisor and the dashboard. Phil's
own files live in the clone on the volume, so its hourly commits never
rebuild or restart the service (`railway.toml` watches `deploy/**`,
`dashboard/**` and `railway.toml` only).

## One-time setup

### 1. Your fork

Phil pushes its commits to `origin`, so `origin` must be yours.

```bash
gh repo fork bennyjo/phil --clone=false
git remote add origin https://github.com/<you>/phil.git
git push --force origin main
```

The force push is intentional and safe on a brand-new fork: it replaces the
fork's copy of upstream `main` with this branch, which already contains all of
upstream's history up to the fresh start plus the operator commits on top.

Then, on GitHub, open the fork's **Actions** tab and enable workflows. The
`CI` workflow is the independent check that no agent commit touches
operator-owned files, and Railway's "Wait for CI" (step 4) builds on it.

### 2. A GitHub token for pushes

Create a **fine-grained personal access token**: GitHub, Settings, Developer
settings, Fine-grained tokens.

- Repository access: **only your fork**
- Permissions: **Contents: Read and write** (nothing else)

The runner pushes with it; the Claude session never sees it (`loop.sh`
removes it from the agent's environment).

### 3. Claude credentials

Pick one:

- **Your Claude plan (Pro/Max):** run `claude setup-token` locally and copy
  the long-lived token into `CLAUDE_CODE_OAUTH_TOKEN`. Cycles count against
  your plan's usage limits.
- **API billing:** an `ANTHROPIC_API_KEY`, ideally from a workspace with a
  monthly spend limit. Cycles are billed per token.

Set exactly one. FULL cycles run on `claude-opus-5-5` and LIGHT ticks on
`claude-sonnet-5` (pinned in `loop.sh`). If your plan lacks Opus, or to cut
cost, override them with `PHIL_MODEL_FULL` / `PHIL_MODEL_LIGHT`.

### 4. Railway

1. **New Project, Deploy from GitHub repo**, pick your fork. Railway finds
   `railway.toml` and builds `deploy/Dockerfile`.
2. **Add a volume** to the service, mounted at **`/data`**. `railway.toml`
   sets `requiredMountPath = "/data"`, so deploys refuse to run without it:
   without the volume, the journal would vanish on every redeploy.
3. **Variables** (next section): at minimum `PHIL_REPO`, `GITHUB_TOKEN`, one
   Claude credential, and `DASHBOARD_PASSWORD`.
4. **Settings, Networking: Generate Domain.** That URL is the dashboard.
5. Optional: **Settings, Source: Wait for CI**, so a runner change deploys
   only after the fork's CI passes.

The first cycle starts about 2 minutes after the service boots. The dashboard's
Runner panel shows each session as it happens.

## Variables

| Variable | Default | What it does |
|---|---|---|
| `PHIL_REPO` | required | Your fork as `owner/repo`; cloned onto the volume on first boot |
| `GITHUB_TOKEN` | required for pushes | Fine-grained PAT (step 2). Without it, commits stay on the volume and the fork never hears about them |
| `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` | required | Claude credentials (step 3) |
| `DASHBOARD_PASSWORD` | unset (open) | HTTP basic auth on the dashboard; any username works |
| `ODDS_API_KEY` | unset | the-odds-api.com key: bookmaker benchmarks for sports markets. Phil skips that research without it |
| `PAUSED` | `0` | `1` = finish the current session, then start no new ones (the dashboard stays up) |
| `CYCLE_INTERVAL_MIN` | `60` | Minutes between hourly cycles (minimum 15) |
| `WATCH_INTERVAL_MIN` | `15` | Minutes between watcher checks; `0` turns TRIGGERED cycles off |
| `DEEP_RETRO_UTC` | `04:40` | Daily deep retro time (UTC); `off` disables it. Skipped on days with nothing to audit |
| `MAX_SESSIONS_PER_DAY` | `36` | Hard cap on Claude sessions per UTC day: 24 hourly + 1 deep retro + up to 6 triggered, plus slack |
| `SESSION_TIMEOUT_MIN` | `90` | A session running longer is stopped |
| `PHIL_MODEL_FULL` / `PHIL_MODEL_LIGHT` | Opus 5.5 / Sonnet 5 | Model overrides for FULL/TRIGGERED/deep-retro and LIGHT ticks |
| `PHIL_GIT_NAME` / `PHIL_GIT_EMAIL` | `Phil (Railway)` | Author of Phil's commits |
| `MTM_REFRESH_S` | `180` | How often the dashboard re-marks open positions from the CLOB (`0` = off) |

Changing a variable redeploys the service. A running session gets up to
10 minutes to finish first (`drainingSeconds = 600`).

## Operating it

- **Dashboard:** the Railway domain. It shows balance and P&L, brier_delta
  (the honest metric), the 100-trade scorecard, first 20 vs last 20,
  calibration, open positions with live Polymarket marks, every trade with
  Phil's rationale, the strategy commits and retros, and the runner's health.
- **Pause / resume:** set `PAUSED=1` / `0`.
- **Run a cycle now:** `railway ssh`, then `touch /data/runtime/run-now`.
  The supervisor picks it up within 20 s, within the daily cap.
- **Logs:** `railway logs` for the supervisor; each session's full output is
  in the dashboard's Runner panel (the Log button), or on the volume under
  `/data/runtime/logs/`.
- **What Phil changed:** `git pull` locally and `git log --oneline`, or the
  dashboard's "What Phil changed about itself" panel.
- **Your own changes:** commit as `operator: ...` (CI fails any other commit
  that touches operator-owned paths), then `git pull --rebase` before pushing,
  because Phil pushes every hour. Runner or dashboard changes redeploy; engine
  changes (`core/`, `CYCLE.md`) reach the running clone on the next cycle's
  sync.
- **Start a new scoreboard:** `python3 deploy/fresh_start.py --label <name> --commit`,
  then push. It archives the journal and keeps the strategy.

## Costs and limits

- Railway: one small always-on service plus a volume of a few hundred MB.
- Claude: about 24 cycles a day. LIGHT ticks are short; FULL cycles fan out
  up to 15 Haiku screening subagents and research on the web, and Phil paces
  itself (`strategy/schedule.json`, at least 4 FULL cycles a day). Watch the
  first days of usage, then tune `CYCLE_INTERVAL_MIN`, the model overrides and
  `MAX_SESSIONS_PER_DAY`.

## Security notes

- The agent reads web pages and market text it does not control. Its tools
  are allowlisted (`loop.sh`), but assume the Claude credential in its
  environment could leak. Scope it: a spend-limited API key, or a token you
  can revoke with `claude setup-token` again.
- The GitHub token never enters the agent's session and can only write your
  fork. If it leaked, the worst case is unwanted commits to the fork, and
  with "Wait for CI" on, an unwanted runner change still has to pass CI.
- The dashboard runs as a separate user with no secrets and serves a fixed
  set of files; set `DASHBOARD_PASSWORD` unless you want it public.

## Troubleshooting

| Symptom (dashboard or logs) | Fix |
|---|---|
| Blocked: no Claude credentials | Set `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` |
| Blocked: PHIL_REPO is not set / clone failed | Set `PHIL_REPO=<owner>/<repo>`; a private fork also needs `GITHUB_TOKEN` |
| "N commits not pushed yet" keeps growing | `GITHUB_TOKEN` missing, expired or without Contents write on the fork |
| Sessions show "Claude failed" | Open the session log: usually auth, usage limits, or a model your plan lacks (use `PHIL_MODEL_FULL`) |
| "Runner silent" | The supervisor stopped writing status: check `railway logs`; the restart policy should have restarted it |
| Deploy never starts | The volume is missing: attach it at `/data` |
