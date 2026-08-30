# ai-status

Small command line tool for AI usage limits. It uses the same direct usage checks as `../home-server`: Codex reads the local `auth.json` files, and Copilot uses the token from `gh auth token`. All three checks run in parallel. The output is updated every 30 seconds.

```bash
python3 ai_status.py
```

Or run it directly from this folder:

```bash
./ai-status
```

The default Codex profile paths are `~/.codex-account1` and `~/.codex-account2`. You can change them with `CODEX1_HOME` and `CODEX2_HOME`.

Run one check only:

```bash
python3 ai_status.py --once
```

## Run from GitHub with uvx

You need [uv](https://docs.astral.sh/uv/) installed. You can run the tool directly from GitHub without cloning the repository:

```bash
uvx --from git+https://github.com/AlexanderLanin/ai-status ai-status
```

Run one check or use a custom interval:

```bash
uvx --from git+https://github.com/AlexanderLanin/ai-status ai-status --once
uvx --from git+https://github.com/AlexanderLanin/ai-status ai-status --interval 60 --timeout 15
```

`uvx` builds the package from GitHub in a temporary environment and runs the `ai-status` command. It uses the local Codex profiles and the GitHub CLI login of the current user.

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

Options:

```text
--interval SECONDS      seconds between checks (default: 30)
--timeout SECONDS       timeout for each HTTP/login request (default: 10)
```

Like the web app, Copilot shows only `Premium interactions`. Codex shows the 5-hour and weekly limits with usage, a progress bar, and the time until reset on the same line. Providers are always printed in the same order, so 30-second snapshots are easy to compare. Tokens are used only in memory for the request. They are never printed or saved.

Tests:

```bash
python3 -m unittest -v
```
