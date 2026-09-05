#!/usr/bin/env python3
"""Read AI usage limits for locally configured command aliases."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

POLL_INTERVAL_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 10.0

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
COPILOT_USAGE_URL = "https://api.github.com/copilot_internal/user"
CODEX_HOME_PATTERN = re.compile(
    r"CODEX_HOME\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s;]+))"
)
PROVIDER_PATTERN = re.compile(r"(?:^|[\s/'=])(?P<provider>codex|copilot)(?:$|[\s/'-])")


class StatusError(RuntimeError):
    """An expected problem while resolving a command or reading usage data."""


@dataclass(frozen=True)
class Target:
    name: str
    provider: str
    codex_home: Path | None = None


@dataclass(frozen=True)
class UsageWindow:
    label: str
    used: float | None
    limit: float | None
    unit: str
    used_percent: float | None
    reset_at: str | None
    unlimited: bool = False


@dataclass(frozen=True)
class UsageResult:
    target: Target
    windows: tuple[UsageWindow, ...] = ()
    error: str | None = None
    available_resets: int | None = None
    next_reset_expires_at: str | None = None


def as_record(value: object) -> dict[str, object] | None:
    return dict(value) if isinstance(value, Mapping) else None


def finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def clamp_percent(value: float) -> float:
    return min(100.0, max(0.0, value))


def epoch_to_iso(value: object) -> str | None:
    seconds = finite_number(value)
    if seconds is None or seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_codex_usage(data: object) -> list[UsageWindow]:
    root = as_record(data)
    rate_limit = as_record(root.get("rate_limit") if root else None)
    if rate_limit is None:
        raise StatusError("Codex response has no rate limits")

    windows: list[UsageWindow] = []
    for seconds_key in ("primary_window", "secondary_window"):
        window = as_record(rate_limit.get(seconds_key))
        if window is None:
            continue
        seconds = finite_number(window.get("limit_window_seconds"))
        used_percent = finite_number(window.get("used_percent"))
        if seconds is None or used_percent is None:
            raise StatusError("Codex limit is incomplete")
        if seconds == 18_000:
            label = "5-hour limit"
        elif seconds == 604_800:
            label = "Weekly limit"
        else:
            label = f"Limit ({round(seconds / 60):g} minutes)"
        used = clamp_percent(used_percent)
        windows.append(
            UsageWindow(
                label=label,
                used=used,
                limit=100.0,
                unit="%",
                used_percent=used,
                reset_at=epoch_to_iso(window.get("reset_at")),
            )
        )
    return windows


def parse_codex_available_resets(data: object) -> int | None:
    root = as_record(data)
    reset_credits = as_record(root.get("rate_limit_reset_credits") if root else None)
    if reset_credits is None:
        return None
    available_count = reset_credits.get("available_count")
    if isinstance(available_count, bool) or not isinstance(available_count, int) or available_count < 0:
        raise StatusError("Codex reset credits are invalid")
    return available_count


def parse_codex_reset_expiry(data: object) -> str | None:
    """Return the earliest expiry of an available Codex reset credit."""
    root = as_record(data)
    credits = root.get("credits") if root else None
    if not isinstance(credits, list):
        raise StatusError("Codex reset credit details are invalid")

    expiries: list[tuple[datetime, str]] = []
    for raw_credit in credits:
        credit = as_record(raw_credit)
        if credit is None:
            continue
        status = credit.get("status")
        if status is not None and status != "available":
            continue
        expires_at = credit.get("expires_at")
        if not isinstance(expires_at, str):
            continue
        expiry = reset_datetime(expires_at)
        if expiry is not None:
            expiries.append((expiry, expires_at))
    return min(expiries)[1] if expiries else None


def parse_copilot_usage(data: object) -> list[UsageWindow]:
    root = as_record(data)
    snapshots = as_record(root.get("quota_snapshots") if root else None)
    if root is None or snapshots is None:
        raise StatusError("Copilot response has no quotas")

    fallback_reset = root.get("quota_reset_date")
    fallback_reset = fallback_reset if isinstance(fallback_reset, str) else None
    windows: list[UsageWindow] = []
    for key, raw in snapshots.items():
        quota = as_record(raw)
        if quota is None:
            raise StatusError(f"Copilot quota {key} is invalid")
        unlimited = quota.get("unlimited") is True
        entitlement = finite_number(quota.get("entitlement"))
        credits_used = finite_number(quota.get("credits_used"))
        percent_remaining = finite_number(quota.get("percent_remaining"))
        if unlimited:
            used_percent = None
        elif percent_remaining is not None:
            used_percent = clamp_percent(100.0 - percent_remaining)
        elif entitlement is not None and entitlement > 0 and credits_used is not None:
            used_percent = clamp_percent(credits_used / entitlement * 100.0)
        else:
            used_percent = None

        has_credits = entitlement is not None and entitlement > 0 and credits_used is not None
        windows.append(
            UsageWindow(
                label={
                    "premium_interactions": "Premium interactions",
                    "chat": "Chat",
                    "completions": "Code completions",
                }.get(key, key.replace("_", " ")),
                used=None if unlimited else credits_used if has_credits else used_percent,
                limit=None if unlimited else entitlement if has_credits else 100.0,
                unit="Credits" if has_credits else "%",
                used_percent=used_percent,
                reset_at=epoch_to_iso(quota.get("quota_reset_at")) or fallback_reset,
                unlimited=unlimited,
            )
        )
    return sorted(windows, key=lambda window: window.label != "Premium interactions")


def fetch_json(url: str, headers: Mapping[str, str]) -> object:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.status < 200 or response.status >= 300:
                raise StatusError(f"HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise StatusError(f"HTTP {exc.code}") from None
    except (TimeoutError, socket.timeout):
        raise StatusError(f"Network timeout after {REQUEST_TIMEOUT_SECONDS:g} s") from None
    except URLError:
        raise StatusError("Network error") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StatusError("Invalid JSON response") from None


def probe_command(name: str) -> str:
    script = """
name="$1"
kind="$(type -t "$name" 2>/dev/null || true)"
printf 'kind=%s\\n' "$kind"
if [ "$kind" = alias ]; then
  alias "$name" 2>/dev/null || true
elif [ "$kind" = function ]; then
  declare -f "$name"
fi
command -v -- "$name" 2>/dev/null || true
"""
    try:
        completed = subprocess.run(
            ["bash", "-ic", script, "ai-status-resolver", name],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise StatusError(f"could not resolve `{name}`: {exc}") from None
    if completed.returncode != 0:
        raise StatusError(f"could not resolve `{name}`")
    return completed.stdout


def target_from_probe(name: str, probe: str) -> Target:
    lines = [line for line in probe.splitlines() if not line.startswith("bash:")]
    kind = next((line.removeprefix("kind=") for line in lines if line.startswith("kind=")), "")
    details = "\n".join(line for line in lines if not line.startswith("kind="))
    if not kind or not details.strip():
        raise StatusError(f"`{name}` was not found in the Bash environment")

    provider_match = PROVIDER_PATTERN.search(details)
    if provider_match is None:
        raise StatusError(f"cannot identify the provider behind `{name}`")
    provider = provider_match.group("provider")
    if provider == "codex":
        home_match = CODEX_HOME_PATTERN.search(details)
        codex_home = None
        if home_match:
            value = next(group for group in home_match.groups() if group is not None)
            codex_home = Path(os.path.expandvars(value)).expanduser()
        return Target(name, provider, codex_home)
    return Target(name, provider)


def resolve_target(name: str) -> Target:
    return target_from_probe(name, probe_command(name))


def codex_auth_paths(home: Path | None) -> list[Path]:
    if home is not None:
        return [home if home.name == "auth.json" else home / "auth.json"]
    default_home = Path.home()
    return [
        default_home / ".config" / "codex" / "auth.json",
        default_home / ".codex" / "auth.json",
    ]


def read_codex_access(home: Path | None) -> tuple[str, str | None]:
    for path in codex_auth_paths(home):
        try:
            auth = as_record(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise StatusError("Cannot read Codex login") from None
        tokens = as_record(auth.get("tokens") if auth else None)
        access_token = tokens.get("access_token") if tokens else None
        account_id = tokens.get("account_id") if tokens else None
        if isinstance(access_token, str) and access_token:
            return access_token, account_id if isinstance(account_id, str) else None
    raise StatusError("Codex login is missing")


def collect_codex(target: Target) -> UsageResult:
    try:
        access_token, account_id = read_codex_access(target.codex_home)
        headers = {
            "Accept": "application/json",
            "User-Agent": "codex-cli",
            "Authorization": f"Bearer {access_token}",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        data = fetch_json(CODEX_USAGE_URL, headers)
        windows = tuple(parse_codex_usage(data))
        available_resets = parse_codex_available_resets(data)
        next_reset_expires_at = None
        if available_resets:
            reset_headers = {
                **headers,
                "OpenAI-Beta": "codex-1",
                "originator": "Codex Desktop",
            }
            try:
                reset_details = fetch_json(CODEX_RESET_CREDITS_URL, reset_headers)
                next_reset_expires_at = parse_codex_reset_expiry(reset_details)
            except StatusError:
                # Keep the usage result visible when the optional details request fails.
                pass
        return UsageResult(
            target,
            windows,
            available_resets=available_resets,
            next_reset_expires_at=next_reset_expires_at,
        )
    except StatusError as exc:
        return UsageResult(target, error=str(exc))


def copilot_token() -> str:
    try:
        completed = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except FileNotFoundError:
        raise StatusError("GitHub CLI `gh` was not found") from None
    except subprocess.TimeoutExpired:
        raise StatusError("GitHub CLI login did not respond") from None
    except OSError:
        raise StatusError("Could not read GitHub CLI login") from None
    if completed.returncode != 0 or not completed.stdout.strip():
        raise StatusError("GitHub CLI login is missing")
    return completed.stdout.strip()


def collect_copilot(target: Target) -> UsageResult:
    try:
        token = copilot_token()
        windows = tuple(
            window
            for window in parse_copilot_usage(
                fetch_json(
                    COPILOT_USAGE_URL,
                    {
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {token}",
                    },
                )
            )
            if window.label == "Premium interactions"
        )
        return UsageResult(target, windows)
    except StatusError as exc:
        return UsageResult(target, error=str(exc))


def collect_usage(target: Target) -> UsageResult:
    if target.provider == "codex":
        return collect_codex(target)
    return collect_copilot(target)


def format_number(value: float | None) -> str:
    if value is None:
        return "–"
    rounded = round(value, 1)
    raw = str(int(rounded)) if rounded.is_integer() else f"{rounded:.1f}"
    whole, _, fraction = raw.partition(".")
    grouped = f"{int(whole):,}"
    return f"{grouped}.{fraction}" if fraction else grouped


def reset_datetime(value: str) -> datetime | None:
    try:
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            return datetime.strptime(value, "%Y-%m-%d").astimezone()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def reset_text(value: str | None, now: datetime) -> str | None:
    if not value:
        return None
    reset = reset_datetime(value)
    if reset is None:
        return None
    remaining = int((reset - now).total_seconds())
    if remaining <= 0:
        return "Reset is due"
    days, rest = divmod(remaining, 86_400)
    hours, rest = divmod(rest, 3_600)
    minutes, seconds = divmod(rest, 60)
    return f"In {days:02d}d {hours:02d}h {minutes:02d}m {seconds:02d}s"


def reset_expiry_text(value: str | None, now: datetime) -> str | None:
    if not value:
        return None
    expiry = reset_datetime(value)
    if expiry is None:
        return None
    countdown = reset_text(value, now)
    if countdown is None:
        return None
    warning = " · WARNING: expires in less than 7 days" if expiry - now < timedelta(days=7) else ""
    return f"{countdown}{warning}"


def progress_bar(percent: float) -> str:
    width = 20
    filled = round(clamp_percent(percent) / 100.0 * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def format_window(window: UsageWindow, now: datetime) -> str:
    if window.unlimited:
        return f"  {window.label:<24} Unlimited"
    line = f"  {window.label:<24} {format_number(window.used)} / {format_number(window.limit)} {window.unit}"
    if window.used_percent is not None:
        severity = " · CRITICAL" if window.used_percent >= 95 else " · WARNING" if window.used_percent >= 80 else ""
        line += f" {progress_bar(window.used_percent)}{severity}"
    reset = reset_text(window.reset_at, now)
    if reset:
        line += f" · {reset}"
    return line


def format_result(result: UsageResult, now: datetime) -> str:
    provider = "Codex" if result.target.provider == "codex" else "GitHub Copilot"
    lines = [f"[{result.target.name}] · {provider}"]
    if result.error:
        lines.append(f"  Error: {result.error}.")
        return "\n".join(lines)
    if result.target.provider == "codex" and result.available_resets is not None:
        reset_line = f"  Quota resets available: {result.available_resets}"
        expiry = reset_expiry_text(result.next_reset_expires_at, now)
        if expiry:
            reset_line += f" · Next expiry: {expiry}"
        lines.append(reset_line)
    if not result.windows:
        lines.append("  No usage limits reported.")
        return "\n".join(lines)
    lines.extend(format_window(window, now) for window in result.windows)
    return "\n".join(lines)


def collect_results(targets: list[Target]) -> list[UsageResult]:
    with ThreadPoolExecutor(max_workers=len(targets)) as executor:
        futures = {executor.submit(collect_usage, target): index for index, target in enumerate(targets)}
        results: list[UsageResult | None] = [None] * len(targets)
        for future in as_completed(futures):
            index = futures[future]
            target = targets[index]
            try:
                results[index] = future.result()
            except Exception:
                results[index] = UsageResult(target, error="unexpected error")
    return [result for result in results if result is not None]


def render_results(results: list[UsageResult]) -> str:
    now = datetime.now().astimezone()
    lines = [f"AI limits · {now:%Y-%m-%d %H:%M:%S %Z}", ""]
    for result in results:
        lines.append(format_result(result, now))
        lines.append("")
    return "\n".join(lines).rstrip()


def show_results(results: list[UsageResult]) -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[H\033[J")
    sys.stdout.write(render_results(results) + "\n")
    sys.stdout.flush()


def enter_live_screen() -> bool:
    if not sys.stdout.isatty():
        return False
    sys.stdout.write("\033[?1049h\033[?25l\033[H\033[J")
    sys.stdout.flush()
    return True


def leave_live_screen(active: bool) -> None:
    if active:
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()


def main() -> int:
    names = sys.argv[1:]
    if not names:
        print("Usage: ai-status COMMAND [COMMAND ...]", file=sys.stderr)
        print("COMMAND is a local Codex or Copilot executable or alias.", file=sys.stderr)
        return 2
    try:
        targets = [resolve_target(name) for name in names]
    except StatusError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    live_screen = enter_live_screen()
    print("AI status monitor started · Ctrl-C to exit", flush=True)
    try:
        while True:
            started = time.monotonic()
            results = collect_results(targets)
            if sys.stdout.isatty():
                while True:
                    show_results(results)
                    remaining = POLL_INTERVAL_SECONDS - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    time.sleep(min(1.0, remaining))
                continue
            show_results(results)
            remaining = POLL_INTERVAL_SECONDS - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        print("\nDone.")
        return 0
    finally:
        leave_live_screen(live_screen)


if __name__ == "__main__":
    raise SystemExit(main())
