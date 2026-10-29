# code-review-automation

Prepares reviews of GitLab merge requests in the background with your own Claude Code review
command, a few minutes after something changes. You read the drafts and decide what gets posted.

The scheduled check costs nothing while your queue is quiet. Every few minutes a small Python
script asks GitLab whether any MR where you're a reviewer has a new push, a new comment or a
newly resolved thread. Claude only starts when the answer is yes, and only for those MRs.

What the review looks for is entirely up to you. The tool runs whatever slash command or prompt
you give it and never sees your checklist. That's deliberate: when every reviewer on a team checks
the same things, everyone misses the same bugs.

**It prepares, you publish.** Findings are saved as GitLab draft comments, which only you can see.
The one thing a pass does publicly is resolve a thread you started, once a new commit fixes it.
In the default mode it can't approve, merge or publish comments: those are blocked by deny rules,
not just discouraged (see [How strong the block is](#how-strong-the-block-is)).

## After a pass: getting to the review

The pass runs in the background, but its work isn't stuck in that session. It's on GitLab.

1. **You get a ping.** A desktop notification like `Review ready: my-group/app` /
   `!42: 3 drafts, 1 resolved. Open the MR to publish.` A pass that found nothing sends nothing.
2. **`status` lists what's waiting:** each MR with drafts, its link, and the command to reopen the
   session behind it.
3. **Open the MR in GitLab.** The drafts show as pending comments on the diff, visible only to you.
   Edit or delete any of them, then **Submit review** to publish. Approve in GitLab as usual if
   it's ready. Nothing on the MR changes for the author until you submit.
4. **Ask why.** To question a finding, reopen the pass's session from the repo:
   `claude --resume <session id>` (`status` prints the full command). It has everything the pass
   read and reasoned about.

## What it handles for you

- **Zero-token idle.** Detection is plain `glab api` calls. No LLM call unless something changed.
- **Ignores your own activity.** Your comments and thread resolutions never trigger a pass, so a
  review can't re-trigger itself. Bot accounts you list are ignored the same way.
- **Debounce.** An MR has to sit unchanged for one check before it's reviewed, so five quick
  pushes cost one pass, not five.
- **Incremental context.** Each pass gets the head commit it's reviewing and the head your last
  pass saw, so your command can review only what changed.
- **Retry with a cap.** A failed pass is retried on the next check. After two failures on the same
  state it waits for the MR to change again, so a broken run can't loop all day.
- **Limits.** A max spend per pass (`--max-budget-usd`) and a time limit per pass. A pass pays
  for your repo's `CLAUDE.md` and memory before it does anything, so check the `COST` lines in the
  log after the first few passes and set the limit with some room above them.
- **One run at a time.** A lock stops two checks from overlapping. If a run crashes, the next
  check clears the lock and carries on.
- **Hard limits on what a pass can do.** Deny rules block approving, merging, publishing
  comments, any other `glab api` write and `git push`. Deny rules win over allow rules, including
  your own Claude Code settings.
- **Work hours.** Runs only on the days and hours you pick, and only while the computer is awake.
- **Several repos.** Reviews MRs from every GitLab project you review for, each run from its own
  local clone so that repo's `CLAUDE.md` applies.

## Requirements

- Python 3.9 or newer (standard library only, nothing to `pip install`)
- [GitLab CLI](https://gitlab.com/gitlab-org/cli) (`glab`), logged in: `glab auth login`
- [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`), logged in
- A local clone of each repo you review

## Setup

Put this folder somewhere permanent first (not Downloads). The schedule runs the script from
wherever it was when you ran setup.

macOS: double-click `setup-mac.command`. Windows: double-click `setup-windows.bat`.
Linux, or any OS from a terminal:

```
python3 code_review_automation.py setup
```

The wizard asks for:

1. Your GitLab host (default `gitlab.com`). It reads your username from `glab`.
2. How often to check, which days and which hours.
3. The review command, e.g. `/review-mr {iids} {context}`. It can copy a starter command
   (`examples/review-mr.md`) to `~/.claude/commands/review-mr.md` for you to edit. Fill in its
   "What to look for" section before the first real run.
4. A local clone path for each project currently in your review queue.
5. Max spend per pass, and whether a pass may run any tool (say no to keep approve and merge
   hard-blocked).
6. Whether to show a desktop notification when drafts are ready.
7. Bot accounts whose comments shouldn't trigger a pass.

It then offers to mark everything already in your queue as reviewed (so you don't pay for a
review of every open MR on day one) and to start the schedule.

To set up without questions, write a config file and run `setup --config my-config.json`.
Any key left out uses the default shown in `DEFAULTS` at the top of `code_review_automation.py`.

## Commands

```
python3 code_review_automation.py status              schedule, each MR's state, recent log lines
python3 code_review_automation.py tick --force --dry-run
                                             one check now, print what would run, don't call Claude
python3 code_review_automation.py stop                remove the schedule (config and state are kept)
python3 code_review_automation.py start               reinstall it
python3 code_review_automation.py pause GROUP/REPO 42 skip MR !42 while you review it by hand
python3 code_review_automation.py resume GROUP/REPO 42
python3 code_review_automation.py baseline            mark the whole queue as reviewed at its current state
python3 code_review_automation.py doctor              check glab, claude, repo paths and the schedule
python3 code_review_automation.py draft ... / resolve ...
                                             the helpers a review pass uses (see below)
```

On Windows use `py -3` in place of `python3`.

## Your review command

Each pass runs, from the project's local clone:

```
claude -p "<your command>" --output-format json --max-budget-usd <limit> \
  --permission-mode dontAsk --allowedTools ... --disallowedTools ...
```

Placeholders in the command: `{iids}` (space-separated MR numbers in this project), `{context}`
(path of the JSON file below), `{project}` (e.g. `my-group/my-repo`) and `{repo}` (local clone
path).

The context file (also in the environment variable `CODE_REVIEW_AUTOMATION_CONTEXT`) looks like:

```json
{
  "project": "my-group/my-repo",
  "project_id": 1234,
  "repo": "/path/to/clone",
  "helper": "python3 /path/to/code_review_automation.py",
  "mrs": [
    {"iid": 42, "web_url": "https://...", "head_sha": "9f1c...", "last_reviewed_head": "3a7d..."}
  ]
}
```

`last_reviewed_head` is the head commit of the last pass that finished, or `null` if none has.
A pass that failed or was skipped doesn't move it, so no code is left out of the next review.
`examples/review-mr.md` shows one way to use it.

**Writing to GitLab.** A pass changes GitLab only through two helper commands, run as
`<helper> ...` with `helper` from the context file:

```
<helper> draft <project_id> <iid> --body "comment"                         # general draft
<helper> draft <project_id> <iid> --path lib/a.py --line 42 --body "..."   # inline draft
<helper> resolve <project_id> <iid> <discussion_id>                        # your own thread only
```

`draft` saves a draft note (private until you submit your review). `resolve` checks who started
the thread and refuses anyone's but yours.

**Permissions.** A pass runs in `dontAsk` mode, so anything not allowed is refused instead of
waiting for a prompt that can't be answered in the background. Allowed by default: reading files, read-only `git`
(`diff`, `log`, `show`, `blame`), `glab mr view`, `glab mr diff`, read-only `glab api`, and the
two helpers. Your own Claude Code settings still apply on top, so anything you've allowed there
is allowed here too. Denied always: `glab mr approve/merge/note/close/create/delete/rebase/
reopen/revoke/update/for/checkout`, `glab api` with `-X`, `--method`, `-f`, `-F` or `--input`,
and `git push`.

If your command needs more (running tests, say), add specific tools to `claude.allowed_tools`.

### How strong the block is

Deny rules match the command a pass runs. They block `glab mr approve` and friends however they're
typed, and they beat any allow rule, so even a broad `Bash(glab mr *)` in your own settings
doesn't reopen approve or merge.

What they can't see is code running inside another command. If a pass may run a script
(`python3 ...`, `bash ...`, `node ...`, `curl ...`), it could call `glab` or the GitLab API from
inside it. So the hard block holds only when:

- `claude.permission_mode` is `dontAsk` (the default), and
- none of your Claude Code settings (`~/.claude/settings*.json` or a repo's
  `.claude/settings*.json`) has an open-ended allow rule for an interpreter or shell, like
  `Bash(python3 *)`, `Bash(bash:*)` or bare `Bash`.

`doctor` checks both and lists any rule that breaks it. With `bypassPermissions` ("let a pass run
any tool" in setup) or such a rule in place, approve and merge are only kept off by your
command's instructions.

## Config reference

`~/.code-review-automation/config.json` (Windows: `%USERPROFILE%\.code-review-automation\config.json`):

| Key | Default | Meaning |
|---|---|---|
| `gitlab_host` | `gitlab.com` | GitLab host, for self-managed instances |
| `username` | from `glab` | your GitLab username |
| `interval_minutes` | `5` | how often the scheduler runs a check |
| `days` | `[0,1,2,3,4]` | days to run, Monday = 0 |
| `start` / `end` | `08:00` / `17:00` | hours to run, 24h local time |
| `debounce_minutes` | `4` | how long an MR must sit unchanged before a pass |
| `max_failures` | `2` | failed passes on the same state before giving up on it |
| `ignore_authors` | `[]` | usernames (bots) whose activity never triggers a pass |
| `default_command` | `/review-mr {iids} {context}` | command for projects without their own |
| `repos` | `{}` | `{"group/repo": {"path": "...", "command": "optional override"}}` |
| `claude.budget_usd` | `2.0` | max spend per pass |
| `claude.timeout_minutes` | `25` | a pass running longer is stopped and retried |
| `claude.permission_mode` | `dontAsk` | `bypassPermissions` lets a pass use any tool not denied (and weakens the block, see above) |
| `claude.allowed_tools` | read-only git and glab, read tools | tools a pass may use (the two helpers are always added) |
| `claude.denied_tools` | approve, merge, publish, API writes, `git push` | tools a pass may never use |
| `claude.extra_args` | `[]` | anything else to pass to `claude`, e.g. `["--model", "sonnet"]` |
| `notify_command` | `desktop` if chosen in setup | `desktop` = built-in notification; or any shell command, run with `CODE_REVIEW_AUTOMATION_TITLE` and `CODE_REVIEW_AUTOMATION_MESSAGE` set. Fires when a pass saved drafts or failed |
| `path_env` | your `PATH` at setup | `PATH` used by scheduled runs, so they find `glab`, `claude` and `git` |

`desktop` uses Notification Center on macOS, a toast on Windows, and `notify-send` on Linux (if
installed). A custom command can send the same text anywhere else, e.g. a chat webhook with `curl`.

## How it's scheduled

| OS | Scheduler | Where |
|---|---|---|
| macOS | LaunchAgent | `~/Library/LaunchAgents/code-review-automation.tick.plist` |
| Linux | cron | one `crontab` line tagged `# code-review-automation` |
| Windows | Task Scheduler | task `code-review-automation`, run with `pythonw.exe` so no window pops up |

On a Windows laptop, Task Scheduler may skip runs on battery by default. If reviews only happen
while plugged in, open Task Scheduler, find `code-review-automation`, and under Conditions untick "Start
the task only if the computer is on AC power".

The scheduler fires every `interval_minutes`, and the script itself checks the days and hours, so
changing the window only needs a config edit. Changing the interval needs `start` again.

macOS uses a LaunchAgent rather than cron because cron jobs run outside your login session and
can't read the Keychain. On Linux, if scheduled runs fail to authenticate with Claude, create a
long-lived token with `claude setup-token` and save it to `~/.code-review-automation/claude-token`
(readable only by you, `chmod 600`). It's passed to each run as `CLAUDE_CODE_OAUTH_TOKEN`.

## Files

Everything lives in `~/.code-review-automation/`:

- `config.json`: your settings
- `snapshot.json`: each MR's last seen and last reviewed state
- `code-review-automation.log`: one line per pass, with the result and cost (`COST - $0.42, 7 turns, ...`)
- `runs/`: context files for the last 50 passes
- `paused/`: one empty file per paused MR

Each pass has a session id in the log (`NEW - ... - session <id>`). Open it with
`claude --resume <id>` from the repo to see exactly what the review did.

## Tests

```
python3 -m unittest discover -s tests
```

Runs against a fake `glab` and `claude`, so nothing touches GitLab or Claude.

## Status

Tested on macOS: the test suite, a real pass started by launchd from a LaunchAgent (Claude login,
reading the context file and `glab` under the default permissions all work there), and live
checks of the permission rules in both `dontAsk` and `bypassPermissions` mode (helper drafts
allowed; every approve, publish and write form denied, even with a broad `glab mr` allow rule in
the repo's settings). The cron and Task Scheduler installs and the Windows toast haven't been run
on a real Linux or Windows machine yet, so expect rough edges there. GitLab only.
