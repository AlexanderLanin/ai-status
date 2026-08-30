# ai-status

Small command line tool for AI usage limits. It checks Codex 1, Codex 2, and GitHub Copilot in parallel every 30 seconds.

```bash
python3 ai_status.py
```

Or run it directly from this folder:

```bash
./ai-status
```

The default Codex profile paths are `~/.codex-account1` and `~/.codex-account2`. You can change them with `CODEX1_HOME` and `CODEX2_HOME`.

## Run from GitHub with uvx

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first. Then run the public repository directly:

```bash
uvx --from git+https://github.com/AlexanderLanin/ai-status.git ai-status
```

`uvx` builds the package in a temporary environment and runs it. The monitor keeps running until you press `Ctrl-C`.

Example output:

```text
AI status monitor started · Ctrl-C to exit

AI limits · 2026-08-30 15:15:20 CEST
Status updated · next check in 30 s · three requests in parallel ...

Codex 1
  5-hour limit              24 / 100 % [#####---------------] · In 52 min. · 0 sec.
  Weekly limit                4 / 100 % [#-------------------] · In 6 days · 19 hr.

Codex 2
  5-hour limit               0 / 100 % [--------------------] · In 2 hr. · 27 min.
  Weekly limit                0 / 100 % [--------------------] · In 6 days · 21 hr.

GitHub Copilot
  Premium interactions     25,000 / 25,000 Credits [####################] · CRITICAL · In 1 day · 8 hr.
```

Like the web app, Copilot shows only `Premium interactions`. Codex shows the 5-hour and weekly limits with usage, a progress bar, and the time until reset on the same line. Providers are always printed in the same order, so 30-second snapshots are easy to compare. Tokens are used only in memory for the request. They are never printed or saved.

Tests:

```bash
python3 -m unittest -v
```
