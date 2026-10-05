# claude-garage

A small, growing collection of standalone utility scripts — each one self-contained, documented,
and usable on its own.

## Tools

### `tools/power_schedule.sh`

Schedules a Mac to wake and sleep at fixed times on chosen days, and disables idle system sleep
in between — useful for keeping a machine reliably on for a scheduled or unattended job (a cron
task, a long-running script, anything that needs the machine awake on a schedule but not
necessarily attended).

Before changing anything, it saves the machine's current sleep settings (battery and AC power can
differ) to a local state file, so running it in reverse restores exactly what was there before —
no guessing, no hardcoded defaults to revert to.

```
./tools/power_schedule.sh enable <wake HH:MM> <sleep HH:MM> [days]
./tools/power_schedule.sh disable
```

Example — wake at 8am, sleep at 5:30pm, weekdays only:
```
./tools/power_schedule.sh enable 08:00 17:30 MTWTF
```

`[days]` uses `pmset`'s day-letter format (`M T W T F S U` for Mon–Sun — e.g. `MTWTF` for
weekdays, `MTWTFSU` for every day). Defaults to weekdays if omitted. Requires `sudo` — run it
directly in a terminal, not from something that can't answer an interactive password prompt.

### `tools/claude_statusline.sh`

A Claude Code status line showing the current directory, git branch (with a `✗` when the tree is
dirty), model and effort level, and how many tokens are in the context window:

```
➜  my-repo  git:(main) ✗  Opus 5.5·medium  ctx 85k
```

Every turn re-reads the whole context (cheaply, from the prompt cache, but the cost scales with its
size), so `ctx` turns yellow above 150k tokens and red above 300k, a cue to `/compact` when
continuing the same task or `/clear` when starting a new one. It shows a raw token count rather
than a percentage because percentages hide size on large context windows (30% of 1M is 300k
tokens per turn). Everything comes from the JSON Claude Code already passes to the status line, so
it runs locally and costs no tokens. Requires `jq`.

Setup — copy the script somewhere stable and point `~/.claude/settings.json` at it:

```json
{
  "statusLine": { "type": "command", "command": "bash ~/.claude/claude_statusline.sh" }
}
```

**Windows:** Claude Code runs status line commands through Git Bash when it's installed, so the same
script should run there (not yet tested on a Windows machine). Two differences: Git Bash doesn't
come with `jq` (`winget install jqlang.jq`), and paths in `settings.json` must use forward slashes
(`~` works). Without Git Bash, Claude Code falls back to PowerShell and this Bash script won't run.

**Hiding it** (screen sharing, presentations): the script prints nothing while
`~/.claude/.statusline-off` exists. A toggle alias for `~/.zshrc` (or Oh My Zsh's
`$ZSH_CUSTOM/aliases.zsh`) — `slt` for "status line toggle":

```bash
alias slt='f=~/.claude/.statusline-off; [ -f $f ] && rm $f || touch $f'
```

From inside a Claude Code session, the `!` prefix runs a shell command directly without a model
turn. Shell aliases aren't loaded in that shell (`! slt` gives `command not found`), so use the
commands themselves:

```
! touch ~/.claude/.statusline-off    # hide
! rm ~/.claude/.statusline-off       # show
```
