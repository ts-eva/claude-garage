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
