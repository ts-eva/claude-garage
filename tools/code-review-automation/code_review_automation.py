#!/usr/bin/env python3
"""code-review-automation: prepare GitLab MR reviews in the background with your own Claude Code command.

A scheduler (launchd, cron or Windows Task Scheduler) runs `tick` every few minutes.
Each tick asks GitLab, with no LLM involved, whether any MR in your reviewer queue has
something new: a push, a comment or a resolved thread from someone other than you. Only
then does it start `claude -p "<your command>"` from that project's local clone. What the
review looks for lives entirely in your command; this script never sees it.

Commands:
  setup [--config FILE]   interactive first-time setup (or load a prepared config file)
  start | stop            install / remove the scheduled tick
  status                  schedule, queue state and recent log lines
  tick [--dry-run] [--force]
                          one detection pass (what the scheduler runs)
  baseline                mark everything currently in the queue as already reviewed
  draft PROJECT_ID IID --body TEXT [--path FILE --line N]
                          save a private draft comment (what a review pass posts)
  resolve PROJECT_ID IID DISCUSSION_ID
                          resolve a thread you started (refuses anyone else's)
  pause PROJECT IID       skip one MR until `resume` (e.g. while you review it by hand)
  resume PROJECT IID
  doctor                  check glab, claude, repo paths and the schedule

Python 3.9+, standard library only. State lives in ~/.code-review-automation
(override with CODE_REVIEW_AUTOMATION_HOME).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

HOME = Path(os.environ.get("CODE_REVIEW_AUTOMATION_HOME", Path.home() / ".code-review-automation"))
CONFIG = HOME / "config.json"
SNAPSHOT = HOME / "snapshot.json"
LOG = HOME / "code-review-automation.log"
LOCK = HOME / "tick.lock"
PAUSED = HOME / "paused"
RUNS = HOME / "runs"
TOKEN_FILE = HOME / "claude-token"
SCRIPT = Path(__file__).resolve()
EXAMPLE_COMMAND = SCRIPT.parent / "examples" / "review-mr.md"

TASK_NAME = "code-review-automation"
LAUNCHD_LABEL = "code-review-automation.tick"
CRON_TAG = "# code-review-automation"
LOG_MAX_BYTES = 2_000_000

DENIED_TOOLS = [
    *(f"Bash(glab mr {c}:*)" for c in ("approve", "merge", "note", "close", "create", "delete",
                                        "rebase", "reopen", "revoke", "update", "for", "checkout")),
    # glab api sends a write when given a method or any field
    *(f"Bash(glab api *{flag}*)" for flag in ("-X", "--method", "-f", "-F", "--input")),
    "Bash(git push:*)",
]

DEFAULTS: dict = {
    "gitlab_host": "gitlab.com",
    "username": "",
    "interval_minutes": 5,
    "days": [0, 1, 2, 3, 4],  # Monday = 0
    "start": "08:00",
    "end": "17:00",
    "debounce_minutes": 4,
    "max_failures": 2,
    "ignore_authors": [],
    "default_command": "/review-mr {iids} {context}",
    "repos": {},  # "group/project": {"path": "...", "command": optional override}
    "claude": {
        "budget_usd": 2.0,
        "timeout_minutes": 25,
        # dontAsk: anything not allowed below is refused, whatever your own settings default to.
        "permission_mode": "dontAsk",
        "allowed_tools": ["Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)",
                          "Bash(git show:*)", "Bash(git blame:*)", "Bash(glab mr view:*)",
                          "Bash(glab mr diff:*)", "Bash(glab api:*)"],
        # Deny rules win over every allow and every mode, bypassPermissions included. They keep a
        # pass from changing anything on GitLab except through the draft/resolve helpers.
        "denied_tools": DENIED_TOOLS,
        "extra_args": [],
    },
    "notify_command": "",
    "path_env": "",
}

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


# ---------------------------------------------------------------- config / logging


def merged(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = merged(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) and k != "repos" else v
    return out


def load_config() -> dict:
    if not CONFIG.exists():
        sys.exit(f"No config at {CONFIG}. Run: python {SCRIPT.name} setup")
    return merged(DEFAULTS, json.loads(CONFIG.read_text(encoding="utf-8")))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def log(msg: str) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > LOG_MAX_BYTES:
        LOG.replace(LOG.with_suffix(".log.1"))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG.open("a", encoding="utf-8") as f:
        for line in str(msg).splitlines() or [""]:
            f.write(f"{stamp} {line}\n")


def in_window(cfg: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now()
    hhmm = now.strftime("%H:%M")
    return now.weekday() in cfg["days"] and cfg["start"] <= hhmm < cfg["end"]


# ---------------------------------------------------------------- processes


def tool_cmd(name: str) -> list[str]:
    """Command prefix for glab/claude. CODE_REVIEW_AUTOMATION_GLAB / _CLAUDE override (tests)."""
    override = os.environ.get(f"CODE_REVIEW_AUTOMATION_{name.upper()}")
    if override:
        return [sys.executable, override] if override.endswith(".py") else [override]
    found = shutil.which(name)
    return [found or name]


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        # os.kill(pid, 0) terminates the process on Windows, so ask the kernel instead.
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_capped(cmd: list[str], timeout: float, cwd: str | None = None,
               env: dict | None = None) -> tuple[int, str, str]:
    """Run cmd; on timeout kill its whole process tree. Returns (rc, stdout, stderr); 124 = timeout."""
    kw: dict = {"cwd": cwd, "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                "stdin": subprocess.DEVNULL, "text": True, "encoding": "utf-8", "errors": "replace"}
    if IS_WINDOWS:
        kw["creationflags"] = 0x00000200 | 0x08000000  # NEW_PROCESS_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kw)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        else:
            import signal

            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(5)
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        out, err = proc.communicate()
        return 124, out, err


# ---------------------------------------------------------------- GitLab


class GitLab:
    def __init__(self, host: str):
        self.host = host

    def api(self, path: str, paginate: bool = False):
        cmd = tool_cmd("glab") + ["api", path, "--hostname", self.host]
        if paginate:
            cmd.append("--paginate")
        try:
            rc, out, err = run_capped(cmd, 120)
        except FileNotFoundError:
            log("ERROR - glab not found on PATH")
            return None
        if rc != 0:
            log(f"ERROR - glab api {path}: {err.strip()[:300]}")
            return None
        # --paginate prints one JSON array per page back to back
        pages, pos, dec = [], 0, json.JSONDecoder()
        while pos < len(out):
            if out[pos].isspace():
                pos += 1
                continue
            obj, pos = dec.raw_decode(out, pos)
            pages.append(obj)
        if not pages:
            return None
        if len(pages) == 1:
            return pages[0]
        return [x for p in pages for x in p]

    def mr(self, pid, iid) -> str:
        return f"projects/{pid}/merge_requests/{iid}"

    def write(self, method: str, path: str, body: dict):
        """JSON write through glab. Only the draft/resolve helpers call this."""
        RUNS.mkdir(parents=True, exist_ok=True)
        body_file = RUNS / f"body-{uuid.uuid4()}.json"
        body_file.write_text(json.dumps(body), encoding="utf-8")
        try:
            rc, out, err = run_capped(tool_cmd("glab") + [
                "api", path, "--hostname", self.host, "-X", method,
                "-H", "Content-Type: application/json", "--input", str(body_file)], 120)
        finally:
            body_file.unlink(missing_ok=True)
        if rc != 0:
            sys.exit(f"GitLab refused {method} {path}: {err.strip()[:300]}")
        return json.loads(out) if out.strip() else {}


def project_path(mr: dict) -> str:
    ref = (mr.get("references") or {}).get("full", "")
    if "!" in ref:
        return ref.rsplit("!", 1)[0]
    url = mr.get("web_url", "")
    return url.split("://", 1)[-1].split("/", 1)[-1].split("/-/")[0] if url else str(mr["project_id"])


def activity_of(discussions: list, ignored: set) -> str:
    """Newest note or thread resolution by someone not in `ignored`. Your own notes are
    always ignored so a review pass never re-triggers itself; updated_at isn't used since
    resolving a thread rewrites it on every note."""
    latest = ""
    for d in discussions:
        for n in d.get("notes", []):
            if n.get("system"):
                continue
            if n["author"]["username"] not in ignored:
                latest = max(latest, n.get("created_at") or "")
            resolver = (n.get("resolved_by") or {}).get("username")
            if n.get("resolved") and resolver and resolver not in ignored:
                latest = max(latest, n.get("resolved_at") or "")
    return latest


def pause_file(project: str, iid) -> Path:
    return PAUSED / f"{project.replace('/', '__')}__{iid}"


def detect(cfg: dict, gl: GitLab, adopt: bool = False, now: int | None = None) -> list[dict]:
    """Update the snapshot and return the MRs to review now. adopt=True marks every MR's
    current state as handled instead (baseline)."""
    now = int(now or time.time())
    me = cfg["username"]
    ignored = {me, *cfg["ignore_authors"]}
    stable = cfg["debounce_minutes"] * 60
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8")) if SNAPSHOT.exists() else {"mrs": {}}
    prev_mrs = snap.get("mrs", {})

    queue = gl.api(f"merge_requests?scope=all&state=opened&reviewer_username={quote(me)}"
                   "&per_page=100", paginate=True)
    if queue is None:
        return []  # GitLab unreachable: keep the old snapshot, try again next tick

    new_mrs: dict = {}
    fire: list[dict] = []
    for mr in queue:
        if mr["author"]["username"] == me:
            continue
        pid, iid = mr["project_id"], mr["iid"]
        key = f"{pid}:{iid}"
        prev = prev_mrs.get(key)
        approvals = gl.api(f"{gl.mr(pid, iid)}/approvals")
        discussions = (gl.api(f"{gl.mr(pid, iid)}/discussions?per_page=100", paginate=True)
                       if approvals is not None else None)
        if approvals is None or discussions is None:
            if prev is not None:
                new_mrs[key] = prev  # transient failure: carry forward unchanged
            continue
        if me in {a["user"]["username"] for a in approvals.get("approved_by", [])}:
            continue  # approved; a new push resets approvals and it comes back

        head, act = mr.get("sha") or "", activity_of(discussions, ignored)
        entry = dict(prev or {"handled_head": None, "handled_activity": None,
                              "reviewed_head": None, "fails": 0})
        entry.update(project=project_path(mr), project_id=pid, iid=iid, web_url=mr.get("web_url", ""))
        if prev is None or (prev.get("head"), prev.get("activity")) != (head, act):
            entry.update(head=head, activity=act, since=now, fails=0)
        if adopt:
            entry.update(handled_head=head, handled_activity=act, reviewed_head=head, fails=0)
            entry.pop("fired", None)
        pending = (head, act) != (entry["handled_head"], entry["handled_activity"])
        paused = pause_file(entry["project"], iid).exists()
        # Debounce: a burst of pushes or replies costs one pass, not one each.
        if pending and not paused and now - entry["since"] >= stable:
            entry["fired"] = [head, act]
            fire.append(dict(entry, key=key))
        new_mrs[key] = entry

    write_json(SNAPSHOT, {"mrs": new_mrs})
    return fire


def settle(keys: list[str], ok: bool, max_failures: int, skipped: bool = False) -> None:
    """ok: the fired state is handled. Failed: retried next tick, until max_failures on the
    same state, so a run that keeps dying can't loop every few minutes. skipped: handled
    without a review (no clone mapped)."""
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for key in keys:
        e = snap["mrs"].get(key)
        if not e or "fired" not in e:
            continue
        if not ok and not skipped:
            e["fails"] = e.get("fails", 0) + 1
            if e["fails"] < max_failures:
                continue
            log(f"WARN - {e['project']}!{e['iid']}: {e['fails']} failed runs on "
                f"{e['fired'][0][:8]}, skipping this state until the MR changes again")
        e["handled_head"], e["handled_activity"] = e.pop("fired")
        if ok:
            # Only a pass that finished moves the incremental base; a skipped or given-up
            # state must not hide its code from the next review.
            e["reviewed_head"] = e["handled_head"]
            e["fails"] = 0
    write_json(SNAPSHOT, snap)


# ---------------------------------------------------------------- review helpers
# A review pass may only change GitLab through these two commands: draft notes stay private
# until you publish them, and resolve only touches threads you started.


def draft(project_id: str, iid: str, body: str, path: str | None, line: int | None,
          old_line: int | None) -> None:
    cfg = load_config()
    gl = GitLab(cfg["gitlab_host"])
    note: dict = {"note": body}
    if path:
        if line is None and old_line is None:
            sys.exit("--path needs --line (new file line) and/or --old-line (removed line)")
        refs = (gl.api(gl.mr(project_id, iid)) or {}).get("diff_refs")
        if not refs:
            sys.exit(f"Couldn't read the diff of !{iid}")
        note["position"] = {"position_type": "text", "base_sha": refs["base_sha"],
                            "start_sha": refs["start_sha"], "head_sha": refs["head_sha"],
                            "new_path": path, "old_path": path}
        if line is not None:
            note["position"]["new_line"] = line
        if old_line is not None:
            note["position"]["old_line"] = old_line
    gl.write("POST", f"{gl.mr(project_id, iid)}/draft_notes", note)
    where = f" on {path}:{line or old_line}" if path else ""
    log(f"DRAFT - project {project_id} !{iid}{where}")
    print(f"Draft saved on !{iid}{where}. It stays private until published in GitLab.")


def resolve(project_id: str, iid: str, discussion_id: str) -> None:
    cfg = load_config()
    gl = GitLab(cfg["gitlab_host"])
    d = gl.api(f"{gl.mr(project_id, iid)}/discussions/{discussion_id}")
    if not d or not d.get("notes"):
        sys.exit(f"Thread {discussion_id} not found on !{iid}")
    starter = d["notes"][0]["author"]["username"]
    if starter != cfg["username"]:
        sys.exit(f"Not resolved: that thread was started by {starter}, not you. "
                 "Only your own threads can be resolved here.")
    if not d["notes"][0].get("resolvable"):
        sys.exit("That thread can't be resolved (it's a plain comment).")
    gl.write("PUT", f"{gl.mr(project_id, iid)}/discussions/{discussion_id}", {"resolved": True})
    log(f"RESOLVED - project {project_id} !{iid} thread {discussion_id}")
    print(f"Resolved thread {discussion_id} on !{iid}.")


# ---------------------------------------------------------------- tick


def acquire_lock() -> bool:
    for _ in range(2):
        try:
            LOCK.mkdir(parents=True)
        except FileExistsError:
            try:
                holder = int((LOCK / "pid").read_text().strip())
            except (OSError, ValueError):
                holder = 0
                if time.time() - LOCK.stat().st_mtime < 60:
                    return False  # another tick is just writing its pid
            if pid_alive(holder):
                return False
            log(f"WARN - lock holder {holder or 'unknown'} is gone, clearing it")
            shutil.rmtree(LOCK, ignore_errors=True)
            continue
        (LOCK / "pid").write_text(str(os.getpid()))
        return True
    return False


def release_lock() -> None:
    try:
        if (LOCK / "pid").read_text().strip() == str(os.getpid()):
            shutil.rmtree(LOCK, ignore_errors=True)
    except OSError:
        pass


def claude_env(cfg: dict, context_file: Path) -> dict:
    env = dict(os.environ)
    if cfg.get("path_env"):
        env["PATH"] = cfg["path_env"]
    if TOKEN_FILE.exists() and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = TOKEN_FILE.read_text(encoding="utf-8").strip()
    env["CODE_REVIEW_AUTOMATION_CONTEXT"] = str(context_file)
    return env


def claude_cmd(cfg: dict, prompt: str, session_id: str) -> list[str]:
    c = cfg["claude"]
    cmd = tool_cmd("claude") + ["-p", prompt, "--output-format", "json",
                                "--session-id", session_id, "--add-dir", str(RUNS),
                                "--max-budget-usd", str(c["budget_usd"])]
    if c.get("permission_mode"):
        cmd += ["--permission-mode", c["permission_mode"]]
    allowed = list(c.get("allowed_tools", [])) + [f"Bash({helper_cmd()} {a}:*)" for a in ("draft", "resolve")]
    cmd += ["--allowedTools", *allowed]
    if c.get("denied_tools"):
        cmd += ["--disallowedTools", *c["denied_tools"]]
    return cmd + list(c.get("extra_args", []))


def helper_cmd() -> str:
    """How a review pass calls this script's draft/resolve helpers (also its allow rule)."""
    def q(path: Path) -> str:
        text = path.as_posix()  # forward slashes work in Git Bash on Windows too
        return f'"{text}"' if " " in text else text
    return f"{q(Path(sys.executable))} {q(SCRIPT)}"


def summarize(stdout: str) -> str:
    try:
        j = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()[:2000] or "(no output)"
    u = j.get("usage") or {}
    return (f"{j.get('result') or '(no result; ' + str(j.get('subtype')) + ')'}\n"
            f"COST - ${j.get('total_cost_usd', 0):.2f}, {j.get('num_turns')} turns, "
            f"{(j.get('duration_ms') or 0) // 1000}s, out {u.get('output_tokens')} tok")


WINDOWS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text')
$t.Item(0).AppendChild($x.CreateTextNode($env:CODE_REVIEW_AUTOMATION_TITLE)) > $null
$t.Item(1).AppendChild($x.CreateTextNode($env:CODE_REVIEW_AUTOMATION_MESSAGE)) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def desktop_notify_cmd(title: str, message: str) -> list[str] | None:
    if IS_MAC:
        return ["osascript", "-e", "on run argv", "-e",
                "display notification (item 2 of argv) with title (item 1 of argv)",
                "-e", "end run", title, message]
    if IS_WINDOWS:
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_TOAST]
    return ["notify-send", title, message] if shutil.which("notify-send") else None


def notify(cfg: dict, title: str, message: str) -> None:
    """notify_command "desktop" = built-in OS notification; anything else is a shell command."""
    how = cfg.get("notify_command")
    if not how:
        return
    env = dict(os.environ, CODE_REVIEW_AUTOMATION_TITLE=title, CODE_REVIEW_AUTOMATION_MESSAGE=message[:300])
    cmd = desktop_notify_cmd(title, message[:300]) if how == "desktop" else how
    if not cmd:
        return
    try:
        subprocess.run(cmd, shell=isinstance(cmd, str), env=env, timeout=30,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass


def actions_since(offset: int, project_id) -> dict[int, list[int]]:
    """{iid: [drafts, resolved]} from helper log lines written after `offset`."""
    counts: dict[int, list[int]] = {}
    if not LOG.exists():
        return counts
    with LOG.open("rb") as f:
        f.seek(offset)
        text = f.read().decode("utf-8", errors="replace")
    for line in text.splitlines():
        for i, kind in enumerate(("DRAFT", "RESOLVED")):
            tag = f" {kind} - project {project_id} !"
            if tag in line:
                iid = int(line.split(tag, 1)[1].split()[0])
                counts.setdefault(iid, [0, 0])[i] += 1
    return counts


def record_session(keys: list[str], sid: str) -> None:
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for key in keys:
        if key in snap["mrs"]:
            snap["mrs"][key]["last_session"] = sid
    write_json(SNAPSHOT, snap)


def review_project(cfg: dict, project: str, mrs: list[dict], dry_run: bool) -> None:
    keys = [m["key"] for m in mrs]
    iids = " ".join(str(m["iid"]) for m in mrs)
    repo = cfg["repos"].get(project) or {}
    if not repo.get("path"):
        log(f"SKIP - {project} !{iids}: no local clone mapped (add it with `setup`)")
        settle(keys, ok=False, max_failures=cfg["max_failures"], skipped=True)
        return
    if not Path(repo["path"]).is_dir():
        log(f"ERROR - {project}: {repo['path']} does not exist")
        settle(keys, ok=False, max_failures=cfg["max_failures"])
        return
    template = repo.get("command") or cfg["default_command"]
    # str.replace, not format(): a prompt may legitimately contain other braces.
    sid = str(uuid.uuid4())
    RUNS.mkdir(parents=True, exist_ok=True)
    context = RUNS / f"{sid}.json"
    prompt = (template.replace("{iids}", iids).replace("{project}", project)
              .replace("{repo}", repo["path"]).replace("{context}", str(context)))
    write_json(context, {"project": project, "project_id": mrs[0]["project_id"],
                         "repo": repo["path"], "helper": helper_cmd(), "mrs": [
        {"iid": m["iid"], "web_url": m["web_url"], "head_sha": m["head"],
         "last_reviewed_head": m.get("reviewed_head")} for m in mrs]})
    if dry_run:
        log(f"DRY_RUN - {project} !{iids}: would run claude -p {prompt!r} in {repo['path']}")
        return

    log(f"NEW - {project} !{iids} - session {sid}")
    record_session(keys, sid)
    log_start = LOG.stat().st_size
    timeout = cfg["claude"]["timeout_minutes"] * 60
    try:
        rc, out, err = run_capped(claude_cmd(cfg, prompt, sid), timeout, cwd=repo["path"],
                                  env=claude_env(cfg, context))
    except FileNotFoundError:
        rc, out, err = 127, "", "claude not found on PATH"
    if err.strip():
        log(f"STDERR - {err.strip()[:1000]}")
    if rc == 124:
        log(f"WARN - claude ran past {timeout}s and was stopped")
    summary = summarize(out)
    for line in summary.splitlines():
        log(f"RESULT - {project} !{iids} | {line}")
    if rc == 0:
        done = actions_since(log_start, mrs[0]["project_id"])
        lines = [f"!{iid}: {d} draft{'s' * (d != 1)}" + (f", {r} resolved" if r else "")
                 for iid, (d, r) in sorted(done.items())]
        if lines:  # nothing to look at: no ping
            notify(cfg, f"Review ready: {project}", "; ".join(lines) + ". Open the MR to publish.")
    else:
        notify(cfg, f"Review failed: {project} !{iids}", f"exit {rc}. See: code_review_automation.py status")
    settle(keys, ok=rc == 0, max_failures=cfg["max_failures"])
    # Context files are only useful while the run lasts; keep the last 50 for debugging.
    for old in sorted(RUNS.glob("*.json"), key=lambda p: p.stat().st_mtime)[:-50]:
        old.unlink(missing_ok=True)


def tick(dry_run: bool = False, force: bool = False) -> None:
    cfg = load_config()
    if not force and not in_window(cfg):
        return
    if cfg.get("path_env"):
        os.environ["PATH"] = cfg["path_env"]
    if not acquire_lock():
        log("SKIP - previous tick still running")
        return
    try:
        fire = detect(cfg, GitLab(cfg["gitlab_host"]))
        if not fire:
            if datetime.now().minute < cfg["interval_minutes"]:
                log("IDLE - nothing new (hourly heartbeat)")
            return
        by_project: dict[str, list[dict]] = {}
        for m in fire:
            by_project.setdefault(m["project"], []).append(m)
        for project, mrs in by_project.items():
            try:
                review_project(cfg, project, mrs, dry_run)
            except Exception as e:  # noqa: BLE001 - logged and retried, never silently looped
                log(f"ERROR - {project}: {type(e).__name__}: {e}")
                settle([m["key"] for m in mrs], ok=False, max_failures=cfg["max_failures"])
    finally:
        release_lock()


# ---------------------------------------------------------------- scheduler


def tick_argv() -> list[str]:
    exe = Path(sys.executable)
    if IS_WINDOWS:
        quiet = exe.with_name("pythonw.exe")  # no console window flashing every few minutes
        exe = quiet if quiet.exists() else exe
    return [str(exe), str(SCRIPT), "tick"]


def launchd_plist(cfg: dict) -> str:
    from xml.sax.saxutils import escape

    args = "".join(f"<string>{escape(a)}</string>" for a in tick_argv())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>StartInterval</key><integer>{cfg['interval_minutes'] * 60}</integer>
  <key>StandardOutPath</key><string>{HOME / 'scheduler.log'}</string>
  <key>StandardErrorPath</key><string>{HOME / 'scheduler.log'}</string>
</dict></plist>
"""


def cron_line(cfg: dict) -> str:
    q = lambda s: "'" + s.replace("'", "'\\''") + "'"
    cmd = " ".join(q(a) for a in tick_argv())
    return f"*/{cfg['interval_minutes']} * * * * {cmd} >> {q(str(HOME / 'scheduler.log'))} 2>&1 {CRON_TAG}"


def schtasks_create(cfg: dict) -> list[str]:
    tr = " ".join(f'"{a}"' for a in tick_argv())
    return ["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(cfg["interval_minutes"]),
            "/TN", TASK_NAME, "/TR", tr]


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def crontab_lines() -> list[str]:
    out = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    return out.stdout.splitlines() if out.returncode == 0 else []


def schedule_installed() -> bool:
    if IS_WINDOWS:
        return subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME], capture_output=True).returncode == 0
    if IS_MAC:
        return plist_path().exists()
    return any(CRON_TAG in l for l in crontab_lines())


def start() -> None:
    cfg = load_config()
    stop(quiet=True)
    if IS_WINDOWS:
        r = subprocess.run(schtasks_create(cfg), capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"schtasks failed: {r.stderr.strip() or r.stdout.strip()}")
        where = f"Task Scheduler task '{TASK_NAME}'"
    elif IS_MAC:
        # launchd, not cron: cron jobs can't read the login Keychain where tokens often live.
        plist_path().parent.mkdir(parents=True, exist_ok=True)
        plist_path().write_text(launchd_plist(cfg), encoding="utf-8")
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist_path())],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"launchctl bootstrap failed: {r.stderr.strip()}")
        where = f"LaunchAgent {plist_path()}"
    else:
        lines = [l for l in crontab_lines() if CRON_TAG not in l] + [cron_line(cfg)]
        subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, check=True)
        where = "crontab"
    print(f"Scheduled: every {cfg['interval_minutes']} min via {where}.")
    print(f"Reviews only run {cfg['start']}-{cfg['end']} on {days_label(cfg['days'])}, "
          "while the computer is awake.")


def stop(quiet: bool = False) -> None:
    if IS_WINDOWS:
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK_NAME], capture_output=True)
    elif IS_MAC:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(plist_path())],
                       capture_output=True)
        plist_path().unlink(missing_ok=True)
    else:
        lines = crontab_lines()
        if any(CRON_TAG in l for l in lines):
            keep = [l for l in lines if CRON_TAG not in l]
            subprocess.run(["crontab", "-"], input="\n".join(keep) + ("\n" if keep else ""),
                           text=True, check=True)
    if not quiet:
        print("Schedule removed. Config and state are kept in", HOME)


# ---------------------------------------------------------------- status / doctor

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def days_label(days: list[int]) -> str:
    return ",".join(DAY_NAMES[d] for d in sorted(days))


def status() -> None:
    cfg = load_config()
    print(f"Schedule:  {'installed' if schedule_installed() else 'NOT installed (run start)'}, "
          f"every {cfg['interval_minutes']} min, {cfg['start']}-{cfg['end']} {days_label(cfg['days'])}")
    print(f"In window: {'yes' if in_window(cfg) else 'no'}")
    if LOCK.exists():
        print(f"Tick running now (pid {(LOCK / 'pid').read_text().strip() if (LOCK / 'pid').exists() else '?'})")
    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8")) if SNAPSHOT.exists() else {"mrs": {}}
    gl = GitLab(cfg["gitlab_host"])
    if not snap["mrs"]:
        print("Queue:     empty (or no tick has run yet)")
    for e in snap["mrs"].values():
        handled = (e.get("head"), e.get("activity")) == (e.get("handled_head"), e.get("handled_activity"))
        state = ("paused" if pause_file(e["project"], e["iid"]).exists()
                 else "up to date" if handled else "pending")
        drafts = gl.api(f"{gl.mr(e['project_id'], e['iid'])}/draft_notes") if "project_id" in e else None
        waiting = f", {len(drafts)} draft(s) waiting for you" if drafts else ""
        print(f"  {e['project']}!{e['iid']}  {state}{waiting}")
        if drafts:
            print(f"      open: {e.get('web_url')}")
        if e.get("last_session"):
            repo = (cfg["repos"].get(e["project"]) or {}).get("path", "<clone>")
            print(f"      ask about it: cd \"{repo}\" && claude --resume {e['last_session']}")
    if LOG.exists():
        print("\nRecent log:")
        for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]:
            print("  " + line)


# Commands that can run arbitrary code. If your own Claude Code settings allow one, a pass can
# call glab from inside it and approve or merge despite the deny rules. (A broad glab allow rule
# is fine: deny rules beat allow rules, so glab mr approve stays blocked.)
RISKY_COMMANDS = ("python", "py", "node", "deno", "bun", "bash", "sh", "zsh", "fish", "pwsh",
                  "powershell", "cmd", "curl", "wget", "perl", "ruby", "php", "env", "xargs",
                  "npx", "uv", "uvx", "eval", "exec", "source")


def risky_allow_rules(cfg: dict) -> list[str]:
    files = [Path.home() / ".claude" / "settings.json", Path.home() / ".claude" / "settings.local.json"]
    for repo in cfg["repos"].values():
        files += [Path(repo.get("path", "")) / ".claude" / n for n in ("settings.json", "settings.local.json")]
    found = []
    for f in files:
        try:
            rules = json.loads(f.read_text(encoding="utf-8")).get("permissions", {}).get("allow", [])
        except (OSError, ValueError, AttributeError):
            continue
        for rule in rules:
            if rule == "Bash" or rule.startswith("Bash(*"):
                found.append(f"{f}: {rule}")
                continue
            if not rule.startswith("Bash(") or "*" not in rule:
                continue  # an exact command can only ever run that one command
            prefix = rule[5:].rstrip(")").replace(":*", "").rstrip("* ").strip()
            first = Path(prefix.split(" ", 1)[0]).name.lower() if prefix else ""
            if any(first == c or (first.startswith(c) and first[len(c):].lstrip("0123456789.") == "")
                   for c in RISKY_COMMANDS):
                found.append(f"{f}: {rule}")
    return found


def doctor(cfg: dict | None = None) -> bool:
    cfg = cfg or load_config()
    ok = True

    def check(good: bool, label: str, hint: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  [{'ok' if good else '!!'}] {label}" + ("" if good else f"  -> {hint}"))

    print("Checking:")
    check(shutil.which("glab") is not None or "CODE_REVIEW_AUTOMATION_GLAB" in os.environ,
          "glab installed", "install the GitLab CLI: https://gitlab.com/gitlab-org/cli")
    user = GitLab(cfg["gitlab_host"]).api("user")
    check(bool(user) and user.get("username") == cfg["username"],
          f"glab logged in to {cfg['gitlab_host']} as {cfg['username']}",
          f"run: glab auth login --hostname {cfg['gitlab_host']}")
    check(shutil.which("claude") is not None or "CODE_REVIEW_AUTOMATION_CLAUDE" in os.environ,
          "claude installed", "install Claude Code: https://docs.claude.com/en/docs/claude-code")
    for project, repo in cfg["repos"].items():
        check(Path(repo.get("path", "")).is_dir(), f"{project} -> {repo.get('path')}",
              "fix the path with setup")
    check(schedule_installed(), "schedule installed", f"run: python {SCRIPT.name} start")
    if cfg["claude"].get("permission_mode") == "bypassPermissions":
        print("  [..] bypassPermissions is on: a pass can run any tool, so approve and merge are only "
              "kept off by your command's instructions, not a hard block")
    else:
        risky = risky_allow_rules(cfg)
        check(not risky, "no Claude Code allow rule reopens approve/merge",
              "these allow rules let a pass run code that could call glab anyway, so approve and "
              "merge are not hard-blocked while they exist:\n        " + "\n        ".join(risky))
    return ok


# ---------------------------------------------------------------- setup


def ask(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{question}{suffix}: ").strip()
    return answer or default


def ask_valid(question: str, default: str, parse):
    """Ask until parse(answer) succeeds, so a typo doesn't end the wizard."""
    while True:
        try:
            return parse(ask(question, default))
        except ValueError as e:
            print(f"  {e}")


def hhmm(text: str) -> str:
    try:
        return datetime.strptime(text, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError("Use 24h HH:MM, e.g. 08:30.") from None


def positive(cast):
    def parse(text: str):
        try:
            value = cast(text)
        except ValueError:
            raise ValueError("Enter a number.") from None
        if value <= 0:
            raise ValueError("Must be more than 0.")
        return value
    return parse


def days_answer(text: str) -> list[int]:
    days = parse_days(text)
    if not days:
        raise ValueError("Use weekdays, every day, or day names like mon,wed,fri.")
    return days


def ask_yes(question: str, default: bool = True) -> bool:
    answer = ask(f"{question} ({'Y/n' if default else 'y/N'})").lower()
    return default if not answer else answer.startswith("y")


def parse_days(text: str) -> list[int]:
    text = text.strip().lower()
    if text in ("weekdays", "mon-fri"):
        return [0, 1, 2, 3, 4]
    if text in ("every day", "everyday", "all", "mon-sun"):
        return list(range(7))
    names = [d.lower() for d in DAY_NAMES]
    return sorted({names.index(p.strip()[:3]) for p in text.split(",") if p.strip()[:3] in names})


def setup(config_file: str | None) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    if config_file:
        cfg = merged(DEFAULTS, json.loads(Path(config_file).read_text(encoding="utf-8")))
        if not cfg["username"]:
            sys.exit("config needs \"username\"")
        cfg["path_env"] = cfg["path_env"] or os.environ.get("PATH", "")
        write_json(CONFIG, cfg)
        print(f"Saved {CONFIG}")
        doctor(cfg)
        return

    old = merged(DEFAULTS, json.loads(CONFIG.read_text(encoding="utf-8"))) if CONFIG.exists() else dict(DEFAULTS)
    print("code-review-automation setup. Press Enter to keep the value in [brackets].\n")

    host = ask("GitLab host", old["gitlab_host"])
    gl = GitLab(host)
    me = (gl.api("user") or {}).get("username", "")
    if not me:
        sys.exit(f"glab isn't logged in to {host}. Run `glab auth login --hostname {host}` and retry.")
    print(f"Logged in to {host} as {me}.\n")

    print("When should reviews run? (Outside this window ticks exit immediately.)")
    interval = ask_valid("Check every N minutes", str(old["interval_minutes"]), positive(int))
    days = ask_valid("Days (weekdays, every day, or e.g. mon,wed,fri)",
                     days_label(old["days"]).lower(), days_answer)
    start_t = ask_valid("Start time (24h HH:MM)", old["start"], hhmm)
    end_t = ask_valid("End time (24h HH:MM)", old["end"], hhmm)

    print("\nReview command: the Claude Code slash command (or plain prompt) each pass runs.")
    print("Placeholders: {iids} = MR numbers, {context} = path of a JSON file with each MR's")
    print("head and last reviewed commit, {project}, {repo} = local clone path.")
    print("It is YOUR checklist. Reviews catch more when teammates look for different things.")
    command = ask("Command", old["default_command"])
    starter = Path.home() / ".claude" / "commands" / "review-mr.md"
    if command.startswith("/review-mr ") and not starter.exists() and EXAMPLE_COMMAND.exists():
        if ask_yes(f"Copy the starter command to {starter} to edit?"):
            starter.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(EXAMPLE_COMMAND, starter)
            print(f"Copied. Open {starter} and fill in 'What to look for' before the first run.")

    print("\nLocal clones: each pass runs inside the project's clone, so its CLAUDE.md applies.")
    repos = dict(old["repos"])
    queue = gl.api(f"merge_requests?scope=all&state=opened&reviewer_username={quote(me)}&per_page=100") or []
    seen = sorted({project_path(m) for m in queue} | set(repos))
    if seen:
        print("Projects in your review queue now (blank path = skip that project):")
    for project in seen:
        path = ask(f"  {project} local path", (repos.get(project) or {}).get("path", ""))
        if path:
            repos[project] = dict(repos.get(project) or {}, path=str(Path(path).expanduser()))
        else:
            repos.pop(project, None)
    while ask_yes("Add another project (group/name)?", default=not repos):
        project = ask("  project path, e.g. my-group/my-repo")
        path = ask("  local clone path")
        if project and path:
            repos[project] = {"path": str(Path(path).expanduser())}

    print("\nSafety limits for each background pass.")
    budget = ask_valid("Max spend per pass in USD", str(old["claude"]["budget_usd"]), positive(float))
    bypass = ask_yes("Let a pass run any tool (bypassPermissions)? No (recommended) = only reading "
                     "code and MRs, with approve, merge and publishing hard-blocked. Yes = those are "
                     "only kept off by your command's instructions",
                     default=old["claude"]["permission_mode"] == "bypassPermissions")
    notify_on = ask_yes("Desktop notification when drafts are ready?",
                        default=old["notify_command"] in ("", "desktop"))
    ignore = ask("Bot accounts to ignore, comma separated (their comments won't trigger a pass)",
                 ",".join(old["ignore_authors"]))

    cfg = merged(old, {
        "gitlab_host": host, "username": me, "interval_minutes": interval, "days": days,
        "start": start_t, "end": end_t, "default_command": command, "repos": repos,
        "ignore_authors": [a.strip() for a in ignore.split(",") if a.strip()],
        "claude": {"budget_usd": budget, "permission_mode": "bypassPermissions" if bypass else "dontAsk"},
        "notify_command": ("desktop" if notify_on else "") if old["notify_command"] in ("", "desktop")
                          else old["notify_command"],
        "path_env": os.environ.get("PATH", ""),
    })
    write_json(CONFIG, cfg)
    print(f"\nSaved {CONFIG}")

    if not SNAPSHOT.exists() and ask_yes("Treat MRs already in your queue as reviewed, so only new "
                                         "activity triggers a pass?"):
        detect(cfg, gl, adopt=True)
        print("Baseline saved.")
    if ask_yes("Start the schedule now?"):
        start()
    print()
    doctor(cfg)
    print(f"\nTry one pass by hand: python {SCRIPT} tick --force --dry-run")


# ---------------------------------------------------------------- main


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup")
    s.add_argument("--config")
    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--force", action="store_true", help="ignore the day/time window")
    for name in ("start", "stop", "status", "baseline", "doctor"):
        sub.add_parser(name)
    d = sub.add_parser("draft", help="save a private draft comment on an MR (used by review passes)")
    d.add_argument("project_id")
    d.add_argument("iid")
    body = d.add_mutually_exclusive_group(required=True)
    body.add_argument("--body")
    body.add_argument("--body-file")
    d.add_argument("--path", help="file, for an inline comment")
    d.add_argument("--line", type=int, help="line in the new version of the file")
    d.add_argument("--old-line", type=int, help="line in the old version (removed lines)")
    r = sub.add_parser("resolve", help="resolve a thread you started (used by review passes)")
    r.add_argument("project_id")
    r.add_argument("iid")
    r.add_argument("discussion_id")
    for name in ("pause", "resume"):
        x = sub.add_parser(name)
        x.add_argument("project")
        x.add_argument("iid")
    a = p.parse_args()

    if a.cmd == "setup":
        setup(a.config)
    elif a.cmd == "tick":
        tick(a.dry_run, a.force)
    elif a.cmd == "start":
        start()
    elif a.cmd == "stop":
        stop()
    elif a.cmd == "status":
        status()
    elif a.cmd == "doctor":
        sys.exit(0 if doctor() else 1)
    elif a.cmd == "draft":
        text = a.body if a.body is not None else Path(a.body_file).read_text(encoding="utf-8")
        draft(a.project_id, a.iid, text, a.path, a.line, a.old_line)
    elif a.cmd == "resolve":
        resolve(a.project_id, a.iid, a.discussion_id)
    elif a.cmd == "baseline":
        cfg = load_config()
        detect(cfg, GitLab(cfg["gitlab_host"]), adopt=True)
        print("Every MR in the queue is now marked as reviewed at its current state.")
    elif a.cmd == "pause":
        PAUSED.mkdir(parents=True, exist_ok=True)
        pause_file(a.project, a.iid).touch()
        print(f"Paused {a.project}!{a.iid}")
    elif a.cmd == "resume":
        pause_file(a.project, a.iid).unlink(missing_ok=True)
        print(f"Resumed {a.project}!{a.iid}")


if __name__ == "__main__":
    main()
