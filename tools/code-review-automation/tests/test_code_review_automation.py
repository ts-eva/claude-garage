"""Run: python -m unittest discover -s tests   (from the code-review-automation folder)

Everything runs in a temp dir with fake glab and claude, so nothing touches GitLab,
Claude or your real ~/.code-review-automation.
"""
import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import code_review_automation  # noqa: E402

ME = "me"


def mr(iid, sha="aaa", pid=10, author="alice", project="team/app"):
    return {"iid": iid, "project_id": pid, "sha": sha, "author": {"username": author},
            "references": {"full": f"{project}!{iid}"},
            "web_url": f"https://gitlab.example/{project}/-/merge_requests/{iid}"}


def note(author, created, resolved_by=None, resolved_at=None, system=False):
    n = {"author": {"username": author}, "created_at": created, "system": system}
    if resolved_by:
        n.update(resolved=True, resolved_by={"username": resolved_by}, resolved_at=resolved_at)
    return n


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fake = root / "fake"
        self.fake.mkdir()
        self.repo = root / "clone"
        self.repo.mkdir()
        self.env = {
            "CODE_REVIEW_AUTOMATION_HOME": str(root / "state"),
            "CODE_REVIEW_AUTOMATION_GLAB": str(HERE / "fake_glab.py"),
            "CODE_REVIEW_AUTOMATION_CLAUDE": str(HERE / "fake_claude.py"),
            "FAKE_GLAB_DIR": str(self.fake),
        }
        self.old_env = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.m = importlib.reload(code_review_automation)
        self.cfg = self.m.merged(self.m.DEFAULTS, {
            "username": ME, "ignore_authors": ["review-bot"], "path_env": os.environ["PATH"],
            "repos": {"team/app": {"path": str(self.repo)}},
            "days": list(range(7)), "start": "00:00", "end": "23:59",
        })
        self.m.write_json(self.m.CONFIG, self.cfg)
        self.gl = self.m.GitLab("gitlab.com")
        self.now = 1_000_000

    def tearDown(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def put(self, name, data):
        (self.fake / name).write_text(json.dumps(data))

    def detect(self, dt=0, **kw):
        return [m["iid"] for m in self.m.detect(self.cfg, self.gl, now=self.now + dt, **kw)]

    def claude_calls(self):
        f = self.fake / "claude_calls.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


class DetectTest(Base):
    def test_new_mr_waits_for_debounce_then_fires_once(self):
        self.put("queue.json", [mr(1)])
        self.assertEqual(self.detect(0), [])        # first sight: debounce starts
        self.assertEqual(self.detect(239), [])      # still inside the window
        self.assertEqual(self.detect(240), [1])
        self.m.settle(["10:1"], ok=True, max_failures=2)
        self.assertEqual(self.detect(600), [])      # handled, nothing new

    def test_burst_of_pushes_restarts_debounce(self):
        self.put("queue.json", [mr(1, "a")])
        self.detect(0)
        self.put("queue.json", [mr(1, "b")])
        self.assertEqual(self.detect(240), [])      # changed since last look
        self.assertEqual(self.detect(480), [1])

    def test_own_and_ignored_notes_do_not_trigger(self):
        self.put("queue.json", [mr(1)])
        self.detect(0)
        self.detect(240)
        self.m.settle(["10:1"], ok=True, max_failures=2)
        self.put("disc_10_1.json", [{"notes": [note(ME, "2026-01-02"), note("review-bot", "2026-01-03"),
                                               note("alice", "2026-01-04", system=True)]}])
        self.assertEqual(self.detect(500), [])
        self.assertEqual(self.detect(800), [])

    def test_author_reply_triggers(self):
        self.put("queue.json", [mr(1)])
        self.detect(0)
        self.detect(240)
        self.m.settle(["10:1"], ok=True, max_failures=2)
        self.put("disc_10_1.json", [{"notes": [note("alice", "2026-01-05")]}])
        self.detect(500)
        self.assertEqual(self.detect(800), [1])

    def test_thread_resolved_by_author_triggers_but_not_by_me(self):
        self.put("queue.json", [mr(1)])
        self.put("disc_10_1.json", [{"notes": [note(ME, "2026-01-01")]}])
        self.detect(0, adopt=True)
        self.put("disc_10_1.json", [{"notes": [note(ME, "2026-01-01", ME, "2026-01-02")]}])
        self.detect(10)
        self.assertEqual(self.detect(300), [])
        self.put("disc_10_1.json", [{"notes": [note(ME, "2026-01-01", "alice", "2026-01-03")]}])
        self.detect(400)
        self.assertEqual(self.detect(700), [1])

    def test_authored_and_approved_mrs_are_skipped(self):
        self.put("queue.json", [mr(1, author=ME), mr(2)])
        self.put("appr_10_2.json", {"approved_by": [{"user": {"username": ME}}]})
        self.detect(0)
        self.assertEqual(self.detect(300), [])

    def test_same_iid_in_two_projects_is_tracked_separately(self):
        self.put("queue.json", [mr(1, pid=10), mr(1, pid=20, project="team/api")])
        self.detect(0)
        fired = self.m.detect(self.cfg, self.gl, now=self.now + 300)
        self.assertEqual(sorted(m["key"] for m in fired), ["10:1", "20:1"])

    def test_failed_run_retries_then_gives_up(self):
        self.put("queue.json", [mr(1)])
        self.detect(0)
        self.assertEqual(self.detect(300), [1])
        self.m.settle(["10:1"], ok=False, max_failures=2)
        self.assertEqual(self.detect(600), [1])     # one failure: retry
        self.m.settle(["10:1"], ok=False, max_failures=2)
        self.assertEqual(self.detect(900), [])      # second failure: give up on this state
        self.put("queue.json", [mr(1, "new-push")])
        self.detect(1000)
        self.assertEqual(self.detect(1300), [1])    # a new push is reviewed again

    def test_gitlab_outage_keeps_previous_state(self):
        self.put("queue.json", [mr(1)])
        self.detect(0, adopt=True)
        (self.fake / "fail_disc_10_1.json").touch()
        self.detect(300)
        snap = json.loads(self.m.SNAPSHOT.read_text())
        self.assertIn("10:1", snap["mrs"])          # carried forward, not dropped
        (self.fake / "fail_queue.json").touch()
        self.assertEqual(self.detect(600), [])

    def test_baseline_marks_queue_handled(self):
        self.put("queue.json", [mr(1), mr(2)])
        self.detect(0, adopt=True)
        self.assertEqual(self.detect(1000), [])

    def test_paused_mr_does_not_fire_until_resumed(self):
        self.put("queue.json", [mr(1)])
        self.detect(0)
        self.m.PAUSED.mkdir(parents=True)
        self.m.pause_file("team/app", 1).touch()
        self.assertEqual(self.detect(300), [])
        self.m.pause_file("team/app", 1).unlink()
        self.assertEqual(self.detect(301), [1])


class TickTest(Base):
    def ready(self, *mrs):
        self.put("queue.json", list(mrs))
        self.m.detect(self.cfg, self.gl, now=1)     # first sight, long ago: debounce passed

    def test_runs_claude_in_clone_with_command_and_context(self):
        self.cfg["default_command"] = "/review-mr {iids} {context} for {project} {json: {}}"
        self.m.write_json(self.m.CONFIG, self.cfg)
        self.ready(mr(1, "h1"), mr(2, "h2"))
        self.m.tick()
        [call] = self.claude_calls()
        prompt = call["argv"][1]
        self.assertTrue(prompt.startswith("/review-mr 1 2 " + str(self.m.RUNS)))
        self.assertTrue(prompt.endswith(".json for team/app {json: {}}"))
        self.assertEqual(call["argv"][call["argv"].index("--add-dir") + 1], str(self.m.RUNS))
        self.assertEqual(call["context"]["project_id"], 10)
        self.assertEqual(Path(call["cwd"]).resolve(), self.repo.resolve())
        self.assertIn("--allowedTools", call["argv"])
        self.assertEqual(call["argv"][call["argv"].index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(call["context"]["helper"], self.m.helper_cmd())
        self.assertEqual([m["head_sha"] for m in call["context"]["mrs"]], ["h1", "h2"])
        self.assertIn("COST - $0.42", self.m.LOG.read_text())
        self.m.tick()
        self.assertEqual(len(self.claude_calls()), 1)  # settled: no second pass

    def test_failed_claude_is_retried(self):
        os.environ["FAKE_CLAUDE_RC"] = "1"
        try:
            self.ready(mr(1))
            self.m.tick()
            self.m.tick()
        finally:
            os.environ.pop("FAKE_CLAUDE_RC")
        self.assertEqual(len(self.claude_calls()), 2)

    def test_crash_in_one_project_is_retried_not_looped(self):
        self.ready(mr(1))
        real = self.m.claude_env  # glab still works; only starting the pass fails

        def boom(*a, **kw):
            raise PermissionError("Operation not permitted")
        self.m.claude_env = boom
        try:
            self.m.tick()
            self.m.tick()
            self.m.tick()
        finally:
            self.m.claude_env = real
        log = self.m.LOG.read_text()
        self.assertEqual(log.count("ERROR - team/app: PermissionError"), 2)  # then gives up
        self.assertFalse(self.m.LOCK.exists())

    def test_failed_or_skipped_head_is_not_reported_as_reviewed(self):
        os.environ["FAKE_CLAUDE_RC"] = "0"
        self.ready(mr(1, "h1"))
        self.m.tick()                               # h1 reviewed
        os.environ["FAKE_CLAUDE_RC"] = "1"
        self.put("queue.json", [mr(1, "h2")])
        self.m.detect(self.cfg, self.gl, now=1)
        self.m.tick()
        self.m.tick()                               # h2 failed twice: given up
        os.environ["FAKE_CLAUDE_RC"] = "0"
        self.put("queue.json", [mr(1, "h3")])
        self.m.detect(self.cfg, self.gl, now=1)
        self.m.tick()
        os.environ.pop("FAKE_CLAUDE_RC")
        last = self.claude_calls()[-1]["context"]["mrs"][0]
        self.assertEqual((last["head_sha"], last["last_reviewed_head"]), ("h3", "h1"))

    def notified(self):
        f = self.fake / "notified.txt"
        return f.read_text().splitlines() if f.exists() else []

    def use_notify_file(self):
        self.cfg["notify_command"] = (f'echo "$CODE_REVIEW_AUTOMATION_TITLE|$CODE_REVIEW_AUTOMATION_MESSAGE" '
                                      f'>> "{self.fake / "notified.txt"}"')
        self.m.write_json(self.m.CONFIG, self.cfg)

    def test_notifies_with_draft_counts_and_records_session(self):
        self.use_notify_file()
        os.environ["FAKE_DRAFTS"] = "10:1,10:1,10:2"
        try:
            self.ready(mr(1), mr(2))
            self.m.tick()
        finally:
            os.environ.pop("FAKE_DRAFTS")
        self.assertEqual(self.notified(), [
            "Review ready: team/app|!1: 2 drafts; !2: 1 draft. Open the MR to publish."])
        snap = json.loads(self.m.SNAPSHOT.read_text())
        sid = self.claude_calls()[0]["argv"][self.claude_calls()[0]["argv"].index("--session-id") + 1]
        self.assertEqual(snap["mrs"]["10:1"]["last_session"], sid)

    def test_status_lists_waiting_drafts_and_resume(self):
        self.put("drafts_10_1.json", [{"id": 1}, {"id": 2}])
        self.ready(mr(1), mr(2))
        self.m.tick()
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.status()
        text = out.getvalue()
        self.assertIn("team/app!1  up to date, 2 draft(s) waiting for you", text)
        self.assertIn("open: https://gitlab.example/team/app/-/merge_requests/1", text)
        self.assertIn("team/app!2  up to date\n", text)
        self.assertIn("claude --resume ", text)

    def test_no_ping_when_pass_found_nothing(self):
        self.use_notify_file()
        self.ready(mr(1))
        self.m.tick()
        self.assertEqual(self.notified(), [])

    def test_unmapped_project_is_skipped_without_claude(self):
        self.ready(mr(5, pid=99, project="other/thing"))
        self.m.tick()
        self.assertEqual(self.claude_calls(), [])
        self.assertIn("no local clone mapped", self.m.LOG.read_text())

    def test_dry_run_does_not_call_claude(self):
        self.ready(mr(1))
        self.m.tick(dry_run=True)
        self.assertEqual(self.claude_calls(), [])
        self.assertIn("DRY_RUN", self.m.LOG.read_text())

    def test_outside_window_does_nothing(self):
        self.cfg["days"] = []
        self.m.write_json(self.m.CONFIG, self.cfg)
        self.ready(mr(1))
        self.m.tick()
        self.assertEqual(self.claude_calls(), [])
        self.m.tick(force=True)
        self.assertEqual(len(self.claude_calls()), 1)

    def test_live_lock_skips_and_dead_lock_is_cleared(self):
        self.ready(mr(1))
        self.m.LOCK.mkdir(parents=True)
        (self.m.LOCK / "pid").write_text(str(os.getpid()))  # alive: this test process
        self.m.tick()
        self.assertEqual(self.claude_calls(), [])
        (self.m.LOCK / "pid").write_text("999999")          # dead holder
        self.m.tick()
        self.assertEqual(len(self.claude_calls()), 1)
        self.assertFalse(self.m.LOCK.exists())


class HelpersTest(Base):
    def test_window(self):
        cfg = dict(self.cfg, days=[0, 1, 2, 3, 4], start="08:00", end="17:00")
        self.assertTrue(self.m.in_window(cfg, datetime(2026, 1, 5, 8, 0)))    # Monday
        self.assertFalse(self.m.in_window(cfg, datetime(2026, 1, 5, 17, 0)))
        self.assertFalse(self.m.in_window(cfg, datetime(2026, 1, 4, 12, 0)))  # Sunday

    def test_parse_days(self):
        self.assertEqual(self.m.parse_days("weekdays"), [0, 1, 2, 3, 4])
        self.assertEqual(self.m.parse_days("Mon,wed, FRIDAY"), [0, 2, 4])
        self.assertEqual(self.m.parse_days("every day"), list(range(7)))

    def test_schedulers_point_at_tick(self):
        self.assertIn("<integer>300</integer>", self.m.launchd_plist(self.cfg))
        line = self.m.cron_line(self.cfg)
        self.assertTrue(line.startswith("*/5 * * * * ") and line.endswith(self.m.CRON_TAG))
        cmd = self.m.schtasks_create(self.cfg)
        self.assertEqual(cmd[cmd.index("/MO") + 1], "5")
        self.assertTrue(cmd[-1].endswith('"tick"'))

    def test_claude_budget_and_bypass_opt_in(self):
        cfg = self.m.merged(self.cfg, {"claude": {"permission_mode": "bypassPermissions"}})
        cmd = self.m.claude_cmd(cfg, "x", "sid")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(cmd[cmd.index("--max-budget-usd") + 1], "2.0")
        self.assertIn("Bash(glab mr approve:*)", cmd)   # deny rules stay on in bypass mode

    def test_default_permissions_block_gitlab_changes(self):
        cmd = self.m.claude_cmd(self.cfg, "x", "sid")
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--disallowedTools")]
        denied = cmd[cmd.index("--disallowedTools") + 1:]
        self.assertNotIn("Bash(glab:*)", allowed)
        self.assertIn(f"Bash({self.m.helper_cmd()} draft:*)", allowed)
        self.assertIn(f"Bash({self.m.helper_cmd()} resolve:*)", allowed)
        for rule in ("Bash(glab mr approve:*)", "Bash(glab mr merge:*)", "Bash(glab mr note:*)",
                     "Bash(glab api *-X*)", "Bash(glab api *-f*)", "Bash(git push:*)"):
            self.assertIn(rule, denied)

    def test_wizard_reasks_on_bad_input(self):
        import builtins
        answers = iter(["abc", "-3", "7"])
        real_input, builtins.input = builtins.input, lambda _: next(answers)
        try:
            self.assertEqual(self.m.ask_valid("n", "5", self.m.positive(int)), 7)
        finally:
            builtins.input = real_input
        self.assertEqual(self.m.hhmm("9:05"), "09:05")
        with self.assertRaises(ValueError):
            self.m.hhmm("25:00")

    def writes(self):
        f = self.fake / "writes.jsonl"
        return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []

    def test_draft_inline_note_uses_mr_diff_refs(self):
        self.put("mr_10_1.json", {"diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}})
        self.m.draft("10", "1", "is this guarded?", "lib/a.dart", 12, None)
        [w] = self.writes()
        self.assertEqual((w["method"], w["path"]), ("POST", "projects/10/merge_requests/1/draft_notes"))
        self.assertEqual(w["body"]["position"]["new_line"], 12)
        self.assertEqual(w["body"]["position"]["head_sha"], "h")
        self.m.draft("10", "1", "general note", None, None, None)
        self.assertNotIn("position", self.writes()[-1]["body"])

    def test_resolve_only_own_threads(self):
        self.put("thread_abc.json", {"notes": [{"author": {"username": "alice"}, "resolvable": True}]})
        with self.assertRaises(SystemExit):
            self.m.resolve("10", "1", "abc")
        self.assertEqual(self.writes(), [])
        self.put("thread_def.json", {"notes": [{"author": {"username": ME}, "resolvable": True}]})
        self.m.resolve("10", "1", "def")
        [w] = self.writes()
        self.assertEqual((w["method"], w["path"], w["body"]),
                         ("PUT", "projects/10/merge_requests/1/discussions/def", {"resolved": True}))

    def test_risky_allow_rules(self):
        home = Path(self.tmp.name) / "home"
        (home / ".claude").mkdir(parents=True)
        (self.repo / ".claude").mkdir()
        (home / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": [
            "Bash(git diff:*)", "Bash(npm test:*)", "Bash(glab mr view:*)", "Bash(glab api:*)",
            "Read", "Bash(python3.12 -m pytest:*)", "Bash(glab:*)",
            'Bash(python3 -c "print(1)")']}}))  # exact command: not open-ended
        (self.repo / ".claude" / "settings.local.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash", "Bash(curl:*)"]}}))
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            found = [r.split(": ", 1)[1] for r in self.m.risky_allow_rules(self.cfg)]
        finally:
            os.environ["HOME"] = old
        self.assertEqual(found, ["Bash(python3.12 -m pytest:*)", "Bash", "Bash(curl:*)"])

    def test_pid_alive(self):
        self.assertTrue(self.m.pid_alive(os.getpid()))
        self.assertFalse(self.m.pid_alive(999999))


if __name__ == "__main__":
    unittest.main()
