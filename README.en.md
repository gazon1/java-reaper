# java-reaper

A reaper for stale Gradle/Kotlin daemon processes on a workstation: it finds
daemon processes that nobody watches any more and closes them. It is a
standalone tool for one machine — it never touches the repository of the
project you are building.

**Emergency cheat sheet — [RUNBOOK.md](RUNBOOK.md).**

The complete documentation is in Russian: [README.md](README.md). This file is
the short English summary for a foreign reader.

## What it does

- Scans the running processes, recognises Gradle and Kotlin daemons (by the
  command line and by the log file the daemon keeps open) and decides what to
  do with every one of them.
- The decision rules are declarative, not scattered `if`s: the whole policy
  table can be printed with `python3 -m reaper.cli --explain`.
- The base principle is **unknown ⇒ leave it alone**. A daemon is killed only
  when it is proven to be a stale, rebuildable cache that nobody is waiting
  for; a daemon that is not marked as rebuildable is never killed.

## Safety

- **Nothing is killed by default.** The shipped configuration and the systemd
  unit run in `--mode=dry-run`; kill mode has to be requested explicitly with
  `--mode=kill`.
- **Only SIGTERM and SIGKILL**, and only through a `pidfd` — the signal is
  delivered to the very process that was inspected, not to a recycled PID.
  SIGTERM is followed by a grace period, then SIGKILL.
- **PID allowlist**: PIDs listed in `PID_ALLOWLIST` are never touched.
- **The project repository is never modified.** The tool only reads daemon
  logs and the process table, and writes exclusively to its own state
  directory (`~/.local/state/java-reaper`).

## What has actually been measured on a live machine

Honest numbers, taken from the corpus of daemon summaries (since 16.10: 1769
summaries) and from the sweep log (120 sweeps, 20.10):

| class | what happens | is a reaper needed |
|---|---|---|
| Gradle, idle | Gradle removes its own daemons by its idle timeout (3 h): not a single daemon in the corpus idled longer than 5 h between builds | **no** — there is nothing to reap |
| Kotlin | the daemon dies on its own 3–4 min after its last work (10 measurements, p50 = 2.8 min) | **no** |
| Gradle, "stuck" | the client dropped during a build: **81 daemons out of 1559 (5.2 %)**. Gradle does not remove those — its timeout does not act while the daemon is "in a build" | **yes** — the only class where a reaper is needed |
| large daemon under memory pressure | over 120 sweeps free memory was always sufficient (21–29 GB); not a single byte was at stake | rule not verified |

The stuck daemons lived after the cancellation p50 = 2.2 min, p90 = 13.2 min,
max 5.8 h (a lower-bound estimate).

How much memory did the stuck daemons hold? Measured, not guessed: of the 81
stuck daemons only **5 (6.2 %)** outlived the 1 h "stuck" threshold — the rest
died on their own within minutes. Together they held memory for 22.0
daemon-hours over 70.8 h of observation, i.e. **~780 MB on average** (~437 MB
of it above the threshold, 1.6 % of the free memory). The class is real, but
it does not create pressure. And not a single stuck daemon was alive during
the observation window, so the `stuck` kill rule has never been exercised on
a live daemon.

So the honest expectation is: **on this machine the reaper frees ~0 MB**. That
is not a defect — it is the consequence of the programs cleaning up after
themselves. The reaper is useful as insurance against the rare case (one
daemon hanging for a week is 4–15 GB), not as a source of regular savings.

## The pilot verdict

The live pilot runs in dry-run mode and has no candidates at all, so it has
no events to collect:

> TOO LITTLE DATA: no daemon kind has reached 8 observed events — no verdict.
> The kill rules (`stuck`, `kill`) have never fired — the "kill" path is not
> verified on live daemons and the pilot has nothing to collect events from.
> Do not enable kill mode yet.

`--status` and `--pilot` print this rule coverage directly, so an unverified
threshold is visible without running any instrument.

## Requirements

- Python 3.11+ (CI runs 3.11 and 3.12).
- PyYAML is **optional**: `pip install -r requirements.txt`. It is needed only
  to read the targets file; without it the tool works with the built-in
  Gradle and Kotlin targets and warns about that on every run.

## Install and run

```bash
mkdir -p ~/.config/java-reaper
cp config.example ~/.config/java-reaper/config
cp targets.example.yaml ~/.config/java-reaper/targets.yaml
cp config.env.example ~/.config/java-reaper/env     # rollout mode: dry-run
pip install -r requirements.txt                      # PyYAML: targets file

# observation only — nothing is killed:
python3 -m reaper.cli --mode=dry-run
```

`config.env.example` sets `REAPER_ARGS=--mode=dry-run`. Kill mode
(`REAPER_ARGS=--mode=kill`) must be enabled explicitly, after the pilot
metrics have been checked.

Read-only modes (nothing is changed):

```bash
python3 -m reaper.cli --status          # table of daemons and decisions
python3 -m reaper.cli --status --json    # the same, machine-readable
python3 -m reaper.cli --explain          # the policy table
```

## Checks

```bash
./check.sh          # syntax, tests, a live dry-run sweep, every tool
./check.sh --fast   # syntax and tests only
```

CI (`.github/workflows/ci.yml`) runs the same checks on Python 3.11 and 3.12:
the test suite plus a smoke run of every tool on a machine without a config
and without collected data. The tools must say "no data" and exit with code 0
there — missing data is not a failure of a tool.

## Tools

```bash
python3 tools/measure-thresholds.py      # measure the thresholds from the logs
python3 tools/simulate.py                # what the rules would have killed
python3 tools/monthly-report.py          # monthly effect report
python3 tools/analyze-idle.py --json     # retrospective idle analysis
python3 tools/prune-logs.py --archive-only --min-age-hours 0
```

## License

MIT — see [LICENSE](LICENSE).

Copyright 2026 Drobin Max (gazon1).

## Documentation

- [README.md](README.md) — the full documentation (Russian).
- [RUNBOOK.md](RUNBOOK.md) — what to do when something goes wrong (Russian).
