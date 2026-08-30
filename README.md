# ai-status

Small command line tool that runs `/status` for local commands and Bash aliases every 30 seconds.

```bash
./ai-status codex1 codex2 copilot
```

Each argument is treated as a command or Bash alias. The tool calls it as `<argument> /status`. Commands run in parallel and are printed in the same order as the arguments.

Aliases must be available from your `.bashrc`. You can also pass a command with arguments by quoting it:

```bash
./ai-status "codex --profile work" copilot
```

The request timeout is fixed at 10 seconds. The check interval is fixed at 30 seconds. Press `Ctrl-C` to stop.

## Run from GitHub with uvx

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first. Then run the public repository directly:

```bash
uvx --from git+https://github.com/AlexanderLanin/ai-status.git ai-status codex1 codex2 copilot
```

`uvx` builds the package in a temporary environment and runs it. No clone is needed.

Example output:

```text
AI status monitor started · Ctrl-C to exit

AI status · 2026-08-30 15:15:20 CEST
3 command(s) checked in parallel · next check in 30 s ...

[codex1]
  5-hour limit: 24 / 100 %
  Weekly limit: 4 / 100 %

[codex2]
  5-hour limit: 0 / 100 %
  Weekly limit: 0 / 100 %

[copilot]
  Premium interactions: 25,000 / 25,000 Credits
```

The output from each command is shown with terminal control codes removed. Use trusted local commands only because their output may contain sensitive data.

Tests:

```bash
python3 -m unittest -v
```
