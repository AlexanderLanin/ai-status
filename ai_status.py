#!/usr/bin/env python3
"""Run /status for any local command or Bash alias on a fixed interval."""

from __future__ import annotations

import errno
import os
import pty
import re
import select
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

POLL_INTERVAL_SECONDS = 30.0
COMMAND_TIMEOUT_SECONDS = 10.0
ANSI_ESCAPE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])"
)


@dataclass(frozen=True)
class CommandResult:
    command: str
    output: str = ""
    exit_code: int | None = None
    error: str | None = None


def clean_bash_warnings(text: str) -> str:
    ignored = (
        "bash: cannot set terminal process group",
        "bash: no job control in this shell",
    )
    return "\n".join(line for line in text.splitlines() if not line.startswith(ignored))


def stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_status(command: str) -> CommandResult:
    """Run a command with /status through an interactive Bash shell.

    An interactive shell is used so aliases and functions from .bashrc work.
    """
    master_fd = slave_fd = None
    process = None
    output: list[bytes] = []
    try:
        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(
            ["bash", "-ic", f"{command} /status"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = None

        deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
        while True:
            wait_for = max(0.0, deadline - time.monotonic())
            if wait_for == 0:
                stop_process(process)
                return CommandResult(
                    command,
                    decode_output(output),
                    error=f"timed out after {COMMAND_TIMEOUT_SECONDS:g} seconds",
                )

            try:
                readable, _, _ = select.select([master_fd], [], [], min(wait_for, 0.2))
            except InterruptedError:
                continue
            if readable:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not chunk:
                    break
                output.append(chunk)
            elif process.poll() is not None:
                break

        return CommandResult(command, decode_output(output), process.wait())
    except FileNotFoundError:
        return CommandResult(command, error="Bash was not found")
    except OSError as exc:
        return CommandResult(command, error=f"could not start command: {exc}")
    finally:
        if process is not None and process.poll() is None:
            stop_process(process)
        if slave_fd is not None:
            os.close(slave_fd)
        if master_fd is not None:
            os.close(master_fd)


def decode_output(chunks: list[bytes]) -> str:
    text = b"".join(chunks).decode("utf-8", errors="replace")
    text = ANSI_ESCAPE.sub("", text)
    return clean_bash_warnings(text.replace("\r\n", "\n").replace("\r", "\n"))



def format_result(result: CommandResult) -> str:
    lines = [f"[{result.command}]"]
    if result.output:
        lines.extend(f"  {line}" for line in result.output.splitlines())
    if result.error:
        lines.append(f"  Error: {result.error}")
    elif not result.output:
        lines.append("  No output.")

    if result.exit_code not in (None, 0):
        lines.append(f"  Exit code: {result.exit_code}")
    return "\n".join(lines)


def poll_and_print(commands: list[str]) -> None:
    now = datetime.now().astimezone()
    command_word = "command" if len(commands) == 1 else "commands"
    print(f"\nAI status · {now:%Y-%m-%d %H:%M:%S %Z}")
    print(
        f"{len(commands)} {command_word} checked in parallel · "
        f"next check in {POLL_INTERVAL_SECONDS:g} s ...",
        flush=True,
    )

    with ThreadPoolExecutor(max_workers=len(commands)) as executor:
        futures = [executor.submit(run_status, command) for command in commands]
        results = []
        for command, future in zip(commands, futures):
            try:
                results.append(future.result())
            except Exception:
                results.append(CommandResult(command, error="unexpected error"))

    for result in results:
        print(format_result(result))
        print()
    sys.stdout.flush()


def main() -> int:
    commands = sys.argv[1:]
    if not commands:
        print("Usage: ai-status COMMAND [COMMAND ...]", file=sys.stderr)
        print("Each command is called as: COMMAND /status", file=sys.stderr)
        return 2

    print("AI status monitor started · Ctrl-C to exit", flush=True)
    try:
        while True:
            started = time.monotonic()
            poll_and_print(commands)
            remaining = POLL_INTERVAL_SECONDS - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        print("\nDone.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
