#!/usr/bin/env python3
"""Small console monitor for the local Copilot and Codex usage limits.

The web application does not drive the interactive CLIs.  It reads the local
Codex auth files and queries the same usage endpoints directly; Copilot is
queried with the token provided by ``gh auth token``.  This keeps the monitor
independent from terminal UI behaviour and never prints the credentials.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

POLL_INTERVAL_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 10.0
PROVIDER_ORDER = ("codex1", "codex2", "copilot")

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_DASHBOARD_URL = "https://chatgpt.com/codex"
COPILOT_USAGE_URL = "https://api.github.com/copilot_internal/user"
COPILOT_DASHBOARD_URL = "https://github.com/settings/billing"


class UsageError(RuntimeError):
    """An expected problem while reading auth or usage data."""


@dataclass(frozen=True)
class UsageWindow:
    id: str
    label: str
    used: float | None
    limit: float | None
    unit: str
    used_percent: float | None
    reset_at: str | None
    unlimited: bool


@dataclass(frozen=True)
class ProviderResult:
    key: str
    label: str
    windows: tuple[UsageWindow, ...]
    dashboard_url: str
    error: str | None = None


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
        raise UsageError("Codex response has no rate limits")

    windows: list[UsageWindow] = []
    for window_id, key in (("primary", "primary_window"), ("secondary", "secondary_window")):
        window = as_record(rate_limit.get(key))
        if window is None:
            continue
        seconds = finite_number(window.get("limit_window_seconds"))
        used_percent = finite_number(window.get("used_percent"))
        if seconds is None or used_percent is None:
            raise UsageError(f"Codex {window_id} limit is incomplete")
        if seconds == 18_000:
            label = "5-hour limit"
        elif seconds == 604_800:
            label = "Weekly limit"
        else:
            label = f"Limit ({round(seconds / 60):g} minutes)"
        used = clamp_percent(used_percent)
        windows.append(
            UsageWindow(
                id=window_id,
                label=label,
                used=used,
                limit=100.0,
                unit="%",
                used_percent=used,
                reset_at=epoch_to_iso(window.get("reset_at")),
                unlimited=False,
            )
        )
    return windows


COPILOT_LABELS = {
    "premium_interactions": "Premium interactions",
    "chat": "Chat",
    "completions": "Code completions",
}


def parse_copilot_usage(data: object) -> list[UsageWindow]:
    root = as_record(data)
    snapshots = as_record(root.get("quota_snapshots") if root else None)
    if root is None or snapshots is None:
        raise UsageError("Copilot response has no quotas")

    fallback_reset = root.get("quota_reset_date")
    fallback_reset = fallback_reset if isinstance(fallback_reset, str) else None
    windows: list[UsageWindow] = []
    for key, raw in snapshots.items():
        quota = as_record(raw)
        if quota is None:
            raise UsageError(f"Copilot quota {key} is invalid")
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
                id=key,
                label=COPILOT_LABELS.get(key, key.replace("_", " ")),
                used=None if unlimited else credits_used if has_credits else used_percent,
                limit=None if unlimited else entitlement if has_credits else 100.0,
                unit="Credits" if has_credits else "%",
                used_percent=used_percent,
                reset_at=epoch_to_iso(quota.get("quota_reset_at")) or fallback_reset,
                unlimited=unlimited,
            )
        )
    return sorted(windows, key=lambda window: window.id != "premium_interactions")


def fetch_json(url: str, headers: Mapping[str, str], timeout: float) -> object:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status < 200 or response.status >= 300:
                raise UsageError(f"HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise UsageError(f"HTTP {exc.code}") from None
    except (TimeoutError, socket.timeout):
        raise UsageError(f"Network timeout after {timeout:g} s") from None
    except URLError:
        raise UsageError("Network error") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UsageError("Invalid JSON response") from None


def read_codex_access(home: Path, label: str) -> tuple[str, str]:
    try:
        auth = as_record(json.loads((home / "auth.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise UsageError(f"{label} login is missing") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise UsageError(f"Cannot read {label} login") from None

    tokens = as_record(auth.get("tokens") if auth else None)
    access_token = tokens.get("access_token") if tokens else None
    account_id = tokens.get("account_id") if tokens else None
    if not isinstance(access_token, str) or not access_token:
        raise UsageError(f"{label} login is missing")
    if not isinstance(account_id, str) or not account_id:
        raise UsageError(f"{label} login has no account ID")
    return access_token, account_id


def collect_codex(key: str, label: str, home: Path, timeout: float) -> ProviderResult:
    try:
        access_token, account_id = read_codex_access(home, label)
        data = fetch_json(
            CODEX_USAGE_URL,
            {
                "Accept": "application/json",
                "User-Agent": "codex-cli",
                "Authorization": f"Bearer {access_token}",
                "ChatGPT-Account-Id": account_id,
            },
            timeout,
        )
        windows = tuple(parse_codex_usage(data))
        return ProviderResult(key, label, windows, CODEX_DASHBOARD_URL)
    except UsageError as exc:
        return ProviderResult(key, label, (), CODEX_DASHBOARD_URL, str(exc))


def copilot_token(timeout: float) -> str:
    try:
        completed = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            check=False,
            text=True,
            timeout=min(5.0, timeout),
        )
    except FileNotFoundError:
        raise UsageError("GitHub CLI `gh` was not found") from None
    except subprocess.TimeoutExpired:
        raise UsageError("GitHub CLI login did not respond") from None
    except OSError:
        raise UsageError("Could not read GitHub CLI login") from None
    if completed.returncode != 0 or not completed.stdout.strip():
        raise UsageError("GitHub CLI login is missing")
    return completed.stdout.strip()


def collect_copilot(timeout: float) -> ProviderResult:
    try:
        token = copilot_token(timeout)
        data = fetch_json(
            COPILOT_USAGE_URL,
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-API-Version": "2026-03-10",
                "Authorization": f"Bearer {token}",
            },
            timeout,
        )
        windows = tuple(parse_copilot_usage(data))
        return ProviderResult("copilot", "GitHub Copilot", windows, COPILOT_DASHBOARD_URL)
    except UsageError as exc:
        return ProviderResult("copilot", "GitHub Copilot", (), COPILOT_DASHBOARD_URL, str(exc))


def profile_home(name: str, default_name: str) -> Path:
    configured = os.environ.get(name) or os.environ.get(f"AI_STATUS_{name}")
    return Path(configured).expanduser() if configured else Path.home() / default_name


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
            local = datetime.strptime(value, "%Y-%m-%d")
            return local.astimezone()
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
        countdown = "Reset is due"
    else:
        days, rest = divmod(remaining, 86_400)
        hours, rest = divmod(rest, 3_600)
        minutes, seconds = divmod(rest, 60)
        if days:
            countdown = f"In {days} {'day' if days == 1 else 'days'} · {hours} hr."
        elif hours:
            countdown = f"In {hours} hr. · {minutes} min."
        else:
            countdown = f"In {minutes} min. · {seconds} sec."
    return countdown


def progress_bar(percent: float) -> str:
    width = 20
    filled = round(clamp_percent(percent) / 100.0 * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def format_window(window: UsageWindow, now: datetime) -> list[str]:
    if window.unlimited:
        return [f"  {window.label:<24} Unlimited"]

    line = f"  {window.label:<24} {format_number(window.used)} / {format_number(window.limit)} {window.unit}"
    if window.used_percent is not None:
        severity = " · CRITICAL" if window.used_percent >= 95 else " · WARNING" if window.used_percent >= 80 else ""
        line += f" {progress_bar(window.used_percent)}{severity}"
    lines = [line]
    reset = reset_text(window.reset_at, now)
    if reset:
        lines[0] += f" · {reset}"
    return lines


def format_provider(result: ProviderResult, now: datetime) -> str:
    lines = [result.label]
    if result.error:
        lines.append(f"  Error: {result.error}.")
        if result.key == "copilot":
            lines.append("  Hint: check the local login with `gh auth login`.")
        else:
            lines.append(f"  Hint: check the local login with `{result.key} login`.")
    else:
        windows = result.windows if result.key in {"codex1", "codex2"} else tuple(
            window for window in result.windows if window.id == "premium_interactions"
        )
        if not windows:
            label = "premium interactions" if result.key == "copilot" else "limits"
            lines.append(f"  No active {label} reported.")
        else:
            for window in windows:
                lines.extend(format_window(window, now))
    return "\n".join(lines)


def select_providers(arguments: list[str]) -> tuple[str, ...]:
    if not arguments:
        return PROVIDER_ORDER

    unknown = [argument for argument in arguments if argument not in PROVIDER_ORDER]
    if unknown:
        names = ", ".join(unknown)
        raise ValueError(f"unknown provider: {names}; use codex1, codex2, or copilot")
    requested = set(arguments)
    return tuple(key for key in PROVIDER_ORDER if key in requested)


def poll_and_print(codex1_home: Path, codex2_home: Path, providers: tuple[str, ...]) -> None:
    now = datetime.now().astimezone()
    print(f"\nAI limits · {now:%Y-%m-%d %H:%M:%S %Z}")
    print(
        f"Status updated · next check in {POLL_INTERVAL_SECONDS:g} s · "
        "selected providers checked in parallel ...",
        flush=True,
    )

    all_jobs = {
        "copilot": ("GitHub Copilot", lambda: collect_copilot(REQUEST_TIMEOUT_SECONDS)),
        "codex1": (
            "OpenAI Codex 1",
            lambda: collect_codex("codex1", "Codex 1", codex1_home, REQUEST_TIMEOUT_SECONDS),
        ),
        "codex2": (
            "OpenAI Codex 2",
            lambda: collect_codex("codex2", "Codex 2", codex2_home, REQUEST_TIMEOUT_SECONDS),
        ),
    }
    jobs = {key: all_jobs[key] for key in providers}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(job): key for key, (_, job) in jobs.items()}
        results: dict[str, ProviderResult] = {}
        for future in as_completed(futures):
            key = futures[future]
            label = jobs[key][0]
            try:
                result = future.result()
            except Exception:
                result = ProviderResult(key, label, (), "", "unexpected error during the request")
            results[key] = result

    # Keep the output stable even though the HTTP calls finish at different
    # times.  That makes repeated 30-second snapshots easy to scan.
    for key in providers:
        print(format_provider(results[key], now))
        print()
    sys.stdout.flush()


def main() -> int:
    try:
        providers = select_providers(sys.argv[1:])
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    codex1_home = profile_home("CODEX1_HOME", ".codex-account1")
    codex2_home = profile_home("CODEX2_HOME", ".codex-account2")

    print("AI status monitor started · Ctrl-C to exit", flush=True)
    try:
        while True:
            started = time.monotonic()
            poll_and_print(codex1_home, codex2_home, providers)
            remaining = POLL_INTERVAL_SECONDS - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        print("\nDone.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
