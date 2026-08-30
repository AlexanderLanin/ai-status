# ai-status

Small command line tool for AI usage limits. It checks Codex and GitHub Copilot directly through their usage endpoints. It does not start an interactive CLI.

```bash
./ai-status codex1 codex2 copilot
```

The arguments are local executable or alias names. They are used only to discover the provider and its login location:

- a Codex alias can set `CODEX_HOME`, for example `~/.codex-account1`
- a Copilot executable selects the GitHub Copilot endpoint and uses the local GitHub CLI login

The tool reads Codex `auth.json` files and asks the usage endpoints directly. Tokens and account data are never printed or saved. Checks run in parallel every 30 seconds. In a terminal, the display redraws every second so reset countdowns stay current.

## Run from GitHub with uvx

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first. Then run the public repository directly:

```bash
uvx --from git+https://github.com/AlexanderLanin/ai-status.git ai-status codex1 codex2 copilot
```

`uvx` builds the package in a temporary environment and runs it. No clone is needed.

Example output:

```text
AI status monitor started · Ctrl-C to exit

AI limits · 2026-08-30 15:15:20 CEST

[codex1] · Codex
  5-hour limit             24 / 100 % [#####---------------] · In 52 min. · 0 sec.
  Weekly limit              4 / 100 % [#-------------------] · In 6 days · 19 hr.

[codex2] · Codex
  5-hour limit              0 / 100 % [--------------------] · In 2 hr. · 27 min.
  Weekly limit               0 / 100 % [--------------------] · In 6 days · 21 hr.

[copilot] · GitHub Copilot
  Premium interactions     25,000 / 25,000 Credits [####################] · CRITICAL · In 1 day · 8 hr.
```

Tests:

```bash
python3 -m unittest -v
```
