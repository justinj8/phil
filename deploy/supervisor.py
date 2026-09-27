#!/usr/bin/env python3
"""Railway supervisor: runs Phil unattended, one Claude session at a time.

  hourly cycle   ./loop.sh 1                               every CYCLE_INTERVAL_MIN
  watch check    python3 core/watch.py check (no Claude)   every WATCH_INTERVAL_MIN
                 -> "trigger": true starts a TRIGGERED cycle (PHIL_TICK=triggered)
  deep retro     PHIL_TICK=deep-retro ./loop.sh 1          daily at DEEP_RETRO_UTC

Every Claude session goes through loop.sh, so its tool allowlist, its
protected-path revert and its push guard every one of them. This process only
decides WHEN. It never passes --real.

Operator controls (Railway variables unless noted):
  PAUSED=1                   finish the current session, start no new ones
  MAX_SESSIONS_PER_DAY=36    hard cap on Claude sessions per UTC day
  touch $PHIL_RUNTIME/run-now   (railway ssh) start an hourly cycle now

It writes only outside the checkout: $PHIL_RUNTIME/status.json (read by the
dashboard), state.json (schedule and counters that survive restarts) and
logs/<UTC>-<kind>.log (one per session).
"""
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time

APP = pathlib.Path(__file__).resolve().parent
PROMPTS = APP / "prompts"
TICK_S = 20                 # scheduler resolution
HEARTBEAT_S = 15            # status.json refresh while a session runs
MIN_GAP_S = 180             # breathing room between two Claude sessions
LOG_KEEP = 400              # newest session logs kept
HISTORY_KEEP = 60           # sessions kept in state.json
STOP = {"requested": False, "at": None}


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def iso(ts):
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def parse_iso(value):
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


ON_RAILWAY = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))


def log(msg, level="info"):
    """One line per event. On Railway: a structured JSON line on stdout, so
    the log explorer shows the real severity (it paints all of stderr red)."""
    if ON_RAILWAY:
        print(json.dumps({"level": level, "message": f"supervisor: {msg}"}), flush=True)
    else:
        print(f"{iso(utcnow())} supervisor: {msg}", file=sys.stderr, flush=True)


def env_int(name, default, lo=0):
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        return max(lo, int(raw))
    except ValueError:
        log(f"ignoring {name}={raw!r} (not an integer); using {default}", "warn")
        return default


def env_flag(name):
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def parse_hhmm(raw):
    raw = (raw or "").strip().lower()
    if raw in ("", "off", "none", "0", "false"):
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        log(f"ignoring DEEP_RETRO_UTC={raw!r} (want HH:MM); deep retro off", "warn")
        return None
    return dt.time(int(m.group(1)), int(m.group(2)))


class Config:
    def __init__(self):
        data = pathlib.Path(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or "/data")
        self.home = pathlib.Path(os.environ.get("PHIL_HOME") or data / "phil")
        self.runtime = pathlib.Path(os.environ.get("PHIL_RUNTIME") or data / "runtime")
        repo = (os.environ.get("PHIL_REPO") or "").strip()
        repo = re.sub(r"^(https://github\.com/|git@github\.com:)", "", repo)
        self.repo = repo.removesuffix(".git").strip("/")
        self.branch = os.environ.get("PHIL_BRANCH") or "main"
        self.cycle_min = env_int("CYCLE_INTERVAL_MIN", 60, lo=15)
        self.watch_min = env_int("WATCH_INTERVAL_MIN", 15)          # 0 = off
        self.deep_at = parse_hhmm(os.environ.get("DEEP_RETRO_UTC", "04:40"))
        self.max_sessions = env_int("MAX_SESSIONS_PER_DAY", 36, lo=1)
        self.timeout_min = env_int("SESSION_TIMEOUT_MIN", 90, lo=10)
        self.first_delay_min = env_int("FIRST_CYCLE_DELAY_MIN", 2)
        self.drain_s = env_int("DRAIN_WAIT_S", 540)
        self.paused = env_flag("PAUSED")
        self.on_railway = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID"))
        self.volume = bool(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"))
        self.version = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "dev")[:7]

    def public(self):
        return {"cycle_interval_min": self.cycle_min, "watch_interval_min": self.watch_min,
                "deep_retro_utc": self.deep_at.strftime("%H:%M") if self.deep_at else None,
                "max_sessions_per_day": self.max_sessions, "session_timeout_min": self.timeout_min,
                "paused": self.paused, "repo": self.repo or None, "branch": self.branch,
                "runner": os.environ.get("PHIL_RUNNER") or "railway"}


# --- persistent state -----------------------------------------------------------

class State:
    """Schedule and counters on the volume, so a restart never resets the clock
    (a crash loop must not start a fresh cycle on every boot)."""

    def __init__(self, path):
        self.path = path
        raw = {}
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        self.day = raw.get("day")
        self.sessions_today = int(raw.get("sessions_today") or 0)
        self.watch_checks_today = int(raw.get("watch_checks_today") or 0)
        self.watch_fires_today = int(raw.get("watch_fires_today") or 0)
        self.last_hourly = parse_iso(raw.get("last_hourly"))
        self.last_watch_at = parse_iso(raw.get("last_watch_at"))
        self.last_session_end = parse_iso(raw.get("last_session_end"))
        self.deep_done_day = raw.get("deep_done_day")
        self.deep_note = raw.get("deep_note")
        self.last_watch = raw.get("last_watch")
        self.history = raw.get("history") or []

    def roll(self, now):
        today = now.strftime("%Y-%m-%d")
        if self.day != today:
            self.day, self.sessions_today = today, 0
            self.watch_checks_today = self.watch_fires_today = 0

    def save(self):
        write_json_atomic(self.path, {
            "day": self.day, "sessions_today": self.sessions_today,
            "watch_checks_today": self.watch_checks_today, "watch_fires_today": self.watch_fires_today,
            "last_hourly": iso(self.last_hourly), "last_watch_at": iso(self.last_watch_at),
            "last_session_end": iso(self.last_session_end), "deep_done_day": self.deep_done_day,
            "deep_note": self.deep_note, "last_watch": self.last_watch,
            "history": self.history[:HISTORY_KEEP]})


def write_json_atomic(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1) + "\n")
    os.chmod(tmp, 0o644)     # the dashboard reads as another user
    os.replace(tmp, path)


# --- the runner -----------------------------------------------------------------------

class Supervisor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.started = utcnow()
        cfg.runtime.mkdir(parents=True, exist_ok=True)
        (cfg.runtime / "logs").mkdir(exist_ok=True)
        self.state = State(cfg.runtime / "state.json")
        self.current = None
        self.proc = None
        self.blocked = None
        self.warnings = []
        self.hooks = False       # does this checkout's loop.sh know PHIL_TICK?

    # -- git ----------------------------------------------------------------------

    def git(self, *args, check=False, timeout=180, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd or self.cfg.home, capture_output=True,
                              text=True, timeout=timeout, check=check)

    def configure_git(self):
        """Identity and a token-from-environment credential helper, for this user only.

        The token never lands in a file: git asks the helper, the helper reads
        $GITHUB_TOKEN. loop.sh strips that variable from the agent's session,
        so only the runner's own pushes (and the lease ref) can use it.
        """
        name = os.environ.get("PHIL_GIT_NAME") or "Phil (Railway)"
        email = os.environ.get("PHIL_GIT_EMAIL") or "phil-bot@users.noreply.github.com"
        helper = ('!f() { test "$1" = get && test -n "$GITHUB_TOKEN" && '
                  'printf "username=x-access-token\\npassword=%s\\n" "$GITHUB_TOKEN"; }; f')
        for key, value in (("user.name", name), ("user.email", email),
                           ("credential.helper", helper), ("init.defaultBranch", "main"),
                           ("advice.detachedHead", "false")):
            subprocess.run(["git", "config", "--global", key, value], check=False)

    def bootstrap(self):
        cfg = self.cfg
        self.configure_git()
        if cfg.on_railway and not cfg.volume:
            self.warnings.append("No volume attached: the checkout and journal live on ephemeral disk "
                                 "and vanish on redeploy. Attach a volume at /data.")
        if not (cfg.home / ".git").is_dir():
            if not cfg.repo:
                self.blocked = "PHIL_REPO is not set (want <owner>/<repo> of your fork)."
                return
            url = f"https://github.com/{cfg.repo}.git"
            log(f"cloning {url} into {cfg.home}")
            cfg.home.parent.mkdir(parents=True, exist_ok=True)
            out = self.git("clone", "--branch", cfg.branch, url, str(cfg.home), cwd=cfg.home.parent, timeout=900)
            if out.returncode != 0:
                self.blocked = f"git clone of {cfg.repo} failed: {(out.stderr or '').strip()[-300:]}"
                return
        elif cfg.repo:
            want = f"https://github.com/{cfg.repo}.git"
            have = (self.git("remote", "get-url", "origin").stdout or "").strip()
            if have != want:
                log(f"origin {have!r} -> {want!r} (PHIL_REPO changed)")
                self.git("remote", "set-url", "origin", want)
        # No loop can be running at boot: a pid file left by the previous
        # container names a pid this container may have reused (loop.sh would
        # then refuse to start, forever).
        (cfg.home / ".loop.pid").unlink(missing_ok=True)
        try:
            self.hooks = "PHIL_TICK" in (cfg.home / "loop.sh").read_text()
        except OSError:
            self.hooks = False
        if not self.hooks:
            self.warnings.append("This checkout's loop.sh has no PHIL_TICK hooks: watch-triggered cycles and "
                                 "the deep retro are off (hourly cycles still run).")
        settings = pathlib.Path.home() / ".claude" / "settings.json"
        if not settings.exists():
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps({"cleanupPeriodDays": 7}) + "\n")

    def check_blockers(self):
        """Why no session can start right now, or None. self.blocked holds
        checkout problems from bootstrap, which run() retries on its own."""
        if shutil.which("claude") is None:
            return "The claude CLI is not on PATH in this image."
        if not (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")):
            return ("No Claude credentials: set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`, "
                    "uses your Claude plan) or ANTHROPIC_API_KEY (pay per token).")
        return self.blocked

    # -- schedule --------------------------------------------------------------------

    def next_hourly(self):
        s = self.state
        base = (s.last_hourly + dt.timedelta(minutes=self.cfg.cycle_min) if s.last_hourly
                else self.started + dt.timedelta(minutes=self.cfg.first_delay_min))
        if s.last_session_end:
            base = max(base, s.last_session_end + dt.timedelta(seconds=MIN_GAP_S))
        return base

    def next_watch(self):
        if not self.cfg.watch_min or not self.hooks:
            return None
        s = self.state
        base = (s.last_watch_at + dt.timedelta(minutes=self.cfg.watch_min) if s.last_watch_at
                else self.started + dt.timedelta(minutes=self.cfg.watch_min))
        if s.last_session_end:
            base = max(base, s.last_session_end + dt.timedelta(seconds=MIN_GAP_S))
        return base

    def next_deep(self, now):
        if not self.cfg.deep_at or not self.hooks:
            return None
        today = dt.datetime.combine(now.date(), self.cfg.deep_at, tzinfo=dt.timezone.utc)
        if self.state.deep_done_day == now.strftime("%Y-%m-%d") or now < today:
            return today if now < today else today + dt.timedelta(days=1)
        return max(today, (self.state.last_session_end or today) + dt.timedelta(seconds=MIN_GAP_S))

    def anything_to_audit(self):
        """Skip a deep retro when nothing happened since the last one."""
        since = (self.git("log", "-1", "--format=%H", "--grep=^deep-retro:").stdout or "").strip()
        if not since:
            since = (self.git("log", "-1", "--format=%H", "--", "config/run.json").stdout or "").strip()
        rng = [f"{since}..HEAD"] if since else ["-n", "200"]
        out = self.git("log", "--format=%s", *rng).stdout or ""
        return any(line.startswith(("retro", "cycle")) for line in out.splitlines())

    # -- status ----------------------------------------------------------------------------

    def auth_mode(self):
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "API key (pay per token)"
        if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return "Claude plan token"
        return "missing"

    def write_status(self, state_name=None):
        now = utcnow()
        blocked = self.check_blockers()
        if state_name is None:
            if self.current:
                state_name = "running"
            elif STOP["requested"]:
                state_name = "stopping"
            elif self.cfg.paused:
                state_name = "paused"
            elif blocked:
                state_name = "blocked"
            elif self.state.sessions_today >= self.cfg.max_sessions:
                state_name = "capped"
            else:
                state_name = "idle"
        nw, nd = self.next_watch(), self.next_deep(now)
        status = {
            "updated_utc": iso(now), "started_utc": iso(self.started), "version": self.cfg.version,
            "state": state_name, "blocked_reason": blocked if state_name == "blocked" else None,
            "warnings": self.warnings, "current": self.current,
            "next": {"hourly_utc": iso(self.next_hourly()), "watch_utc": iso(nw), "deep_retro_utc": iso(nd)},
            "sessions_today": self.state.sessions_today, "max_sessions_per_day": self.cfg.max_sessions,
            "watch_checks_today": self.state.watch_checks_today,
            "watch_fires_today": self.state.watch_fires_today,
            "deep_note": self.state.deep_note,
            "config": self.cfg.public(), "auth": self.auth_mode(),
            "push": "GITHUB_TOKEN set" if os.environ.get("GITHUB_TOKEN") else "no GITHUB_TOKEN: commits stay on the volume",
            "history": self.state.history[:25], "last_watch": self.state.last_watch,
        }
        try:
            write_json_atomic(self.cfg.runtime / "status.json", status)
        except OSError as e:
            log(f"could not write status.json: {e}", "error")

    # -- work -------------------------------------------------------------------------------

    def cycles_log_len(self):
        try:
            return len((self.cfg.home / "journal" / "cycles.log").read_text().splitlines())
        except OSError:
            return 0

    def summary_since(self, before, head_before, log_text):
        """What the session reported: its cycle-log line, else the subject of
        the newest commit it made (a deep retro logs no cycle line), else the
        last line of its log."""
        try:
            lines = (self.cfg.home / "journal" / "cycles.log").read_text().splitlines()[before:]
            done = [ln for ln in lines if " cycle done:" in ln]
            if done:
                return done[-1].split(" cycle done: ", 1)[-1][:400]
        except OSError:
            pass
        if head_before:
            subjects = (self.git("log", "--format=%s", f"{head_before}..HEAD").stdout or "").splitlines()
            if subjects:
                return subjects[0][:400]
        tail = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
        return tail[-1][:400] if tail else ""

    def run_watch(self):
        now = utcnow()
        self.state.last_watch_at = now
        self.state.watch_checks_today += 1
        # watch.py's double-fire guard reads origin/main; bring it up to date
        # (best effort: a stale ref only weakens that guard, it breaks nothing).
        try:
            self.git("fetch", "--quiet", "origin", self.cfg.branch, timeout=60)
        except subprocess.TimeoutExpired:
            log("watch: git fetch timed out; checking against the local origin/main", "warn")
        verdict = {"trigger": False, "error": "no output"}
        try:
            out = subprocess.run([sys.executable, "core/watch.py", "check"], cwd=self.cfg.home,
                                 capture_output=True, text=True, timeout=180,
                                 env=self.session_env())
            lines = [ln for ln in out.stdout.splitlines() if ln.strip().startswith("{")]
            if lines:
                verdict = json.loads(lines[-1])
        except (subprocess.TimeoutExpired, ValueError, OSError) as e:
            verdict = {"trigger": False, "error": f"{type(e).__name__}: {e}"}
        self.state.last_watch = {"utc": iso(now), "trigger": bool(verdict.get("trigger")),
                                 "keys": verdict.get("keys") or [], "notes": (verdict.get("notes") or [])[:4],
                                 "error": verdict.get("error")}
        self.state.save()
        if verdict.get("trigger"):
            self.state.watch_fires_today += 1
            log(f"watch fired: {verdict.get('keys')}")
            prompt = (PROMPTS / "triggered.md").read_text().replace("{verdict}", json.dumps(verdict, indent=2))
            self.run_session("triggered", prompt)

    def session_env(self):
        env = dict(os.environ, PHIL_RUNNER=os.environ.get("PHIL_RUNNER") or "railway",
                   GIT_TERMINAL_PROMPT="0")
        env.pop("PHIL_TICK", None)
        env.pop("PHIL_TICK_PROMPT", None)
        return env

    def run_session(self, kind, prompt=None):
        cfg, st = self.cfg, self.state
        if st.sessions_today >= cfg.max_sessions:
            log(f"{kind}: daily session cap ({cfg.max_sessions}) reached; skipping", "warn")
            return
        start = utcnow()
        log_path = cfg.runtime / "logs" / f"{start.strftime('%Y%m%dT%H%M%SZ')}-{kind}.log"
        env = self.session_env()
        if kind != "hourly":
            prompt_path = cfg.runtime / f"prompt-{kind}.md"
            prompt_path.write_text(prompt)
            env.update(PHIL_TICK=kind, PHIL_TICK_PROMPT=str(prompt_path))
        if kind == "hourly":
            st.last_hourly = start
        st.sessions_today += 1
        st.save()
        before = self.cycles_log_len()
        head_before = (self.git("rev-parse", "HEAD").stdout or "").strip() or None
        self.current = {"kind": kind, "started_utc": iso(start), "log": log_path.name}
        self.write_status()
        log(f"{kind}: starting (session {st.sessions_today}/{cfg.max_sessions} today), log {log_path.name}")

        timed_out = interrupted = False
        with log_path.open("w") as fh:
            fh.write(f"# {kind} session started {iso(start)} by deploy/supervisor.py\n")
            fh.flush()
            # Own process group, so a timeout can stop loop.sh and everything under it.
            self.proc = subprocess.Popen(["bash", "loop.sh", "1"], cwd=cfg.home, env=env, stdout=fh,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + cfg.timeout_min * 60
            while self.proc.poll() is None:
                time.sleep(HEARTBEAT_S)
                self.write_status()
                if time.monotonic() > deadline:
                    timed_out = True
                    log(f"{kind}: over {cfg.timeout_min} min, stopping it", "warn")
                    self.stop_group()
                elif STOP["requested"] and time.monotonic() - STOP["at"] > cfg.drain_s:
                    interrupted = True
                    log(f"{kind}: shutdown drain ({cfg.drain_s}s) spent, stopping it", "warn")
                    self.stop_group()
            code = self.proc.returncode
            self.proc = None
        end = utcnow()
        st.last_session_end = end
        log_text = log_path.read_text(errors="replace")
        # loop.sh swallows a failed `claude -p` ("cycle N failed; continuing")
        # and still pushes, so its exit code alone can't tell success apart.
        claude_failed = "failed; continuing" in log_text
        st.history.insert(0, {"kind": kind, "started_utc": iso(start), "ended_utc": iso(end),
                              "duration_s": round((end - start).total_seconds()), "exit": code,
                              "claude_failed": claude_failed, "timed_out": timed_out,
                              "interrupted": interrupted, "log": log_path.name,
                              "summary": self.summary_since(before, head_before, log_text)})
        st.save()
        self.current = None
        self.write_status()
        log(f"{kind}: done in {round((end - start).total_seconds())}s, exit {code}"
            + (" (the Claude session failed; see its log)" if claude_failed else ""),
            "warn" if claude_failed or timed_out else "info")
        self.prune_logs()

    def stop_group(self):
        """SIGTERM the session's process group, then SIGKILL after 30 s."""
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def prune_logs(self):
        logs = sorted((self.cfg.runtime / "logs").glob("*.log"))
        for old in logs[:-LOG_KEEP]:
            old.unlink(missing_ok=True)

    # -- main loop -----------------------------------------------------------------------------

    def step(self):
        now = utcnow()
        self.state.roll(now)
        if self.cfg.paused or self.check_blockers() or self.state.sessions_today >= self.cfg.max_sessions:
            return
        run_now = self.cfg.runtime / "run-now"
        if run_now.exists():
            run_now.unlink(missing_ok=True)
            log("run-now requested")
            self.run_session("hourly")
            return
        if now >= self.next_hourly():
            self.run_session("hourly")
            return
        nd = self.next_deep(now)
        if nd and now >= nd:
            if self.anything_to_audit():
                self.state.deep_note = None
                self.run_session("deep-retro", (PROMPTS / "deep-retro.md").read_text())
            else:
                self.state.deep_note = f"{now:%Y-%m-%d}: skipped, nothing to audit since the last deep retro"
                log("deep retro skipped: nothing to audit")
            self.state.deep_done_day = now.strftime("%Y-%m-%d")
            self.state.save()
            return
        nw = self.next_watch()
        if nw and now >= nw:
            self.run_watch()

    def run(self):
        log(f"starting: home={self.cfg.home} repo={self.cfg.repo or '(existing checkout)'} "
            f"interval={self.cfg.cycle_min}m watch={self.cfg.watch_min or 'off'} "
            f"deep={self.cfg.deep_at or 'off'} cap={self.cfg.max_sessions}/day paused={self.cfg.paused}")
        self.write_status("starting")
        self.bootstrap()
        for w in self.warnings:
            log(f"WARNING: {w}", "warn")
        blocked = self.check_blockers()
        if blocked:
            log(f"BLOCKED: {blocked}", "warn")
        retry_at = time.monotonic() + 300
        while not STOP["requested"]:
            try:
                if self.blocked and time.monotonic() > retry_at:
                    # A failed clone is often a transient GitHub or network blip.
                    retry_at = time.monotonic() + 300
                    self.blocked = None
                    self.bootstrap()
                    log(f"bootstrap retry: {self.blocked or 'ok'}", "warn" if self.blocked else "info")
                self.step()
            except Exception as e:  # noqa: BLE001 - one bad tick must not end the runner
                log(f"tick failed: {type(e).__name__}: {e}", "error")
            self.write_status()
            for _ in range(TICK_S):
                if STOP["requested"]:
                    break
                time.sleep(1)
        self.write_status("stopped")
        log("stopped")


def main():
    cfg = Config()
    sup = Supervisor(cfg)

    def on_term(signum, _frame):
        # Railway sends SIGTERM on redeploy. Start nothing new; a running
        # session gets DRAIN_WAIT_S to finish on its own (railway.toml sets
        # drainingSeconds above that), so core/resolve.py is never cut off
        # halfway through rewriting the ledger if it can be helped.
        if not STOP["requested"]:
            STOP.update(requested=True, at=time.monotonic())
            log(f"signal {signum}: draining" + (f" ({sup.current['kind']} still running)" if sup.current else ""))

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    sup.run()


if __name__ == "__main__":
    main()
