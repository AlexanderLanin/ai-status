#!/usr/bin/env python3
"""Small console monitor for the local Copilot and Codex usage limits.

The web application does not drive the interactive CLIs.  It reads the local
Codex auth files and queries the same usage endpoints directly; Copilot is
queried with the token provided by ``gh auth token``.  This keeps the monitor
independent from terminal UI behaviour and never prints the credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_INTERVAL = 30.0
DEFAULT_TIMEOUT = 10.0

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
        raise UsageError("Codex-Antwort enthält keine Rate-Limits")

    windows: list[UsageWindow] = []
    for window_id, key in (("primary", "primary_window"), ("secondary", "secondary_window")):
        window = as_record(rate_limit.get(key))
        if window is None:
            continue
        seconds = finite_number(window.get("limit_window_seconds"))
        used_percent = finite_number(window.get("used_percent"))
        if seconds is None or used_percent is None:
            raise UsageError(f"Codex-{window_id}-Limit ist unvollständig")
        if seconds == 18_000:
            label = "5-Stunden-Limit"
        elif seconds == 604_800:
            label = "Wochenlimit"
        else:
            label = f"Limit ({round(seconds / 60):g} Minuten)"
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
    "premium_interactions": "Premium-Interaktionen",
    "chat": "Chat",
    "completions": "Code-Vervollständigungen",
}


def parse_copilot_usage(data: object) -> list[UsageWindow]:
    root = as_record(data)
    snapshots = as_record(root.get("quota_snapshots") if root else None)
    if root is None or snapshots is None:
        raise UsageError("Copilot-Antwort enthält keine Quoten")

    fallback_reset = root.get("quota_reset_date")
    fallback_reset = fallback_reset if isinstance(fallback_reset, str) else None
    windows: list[UsageWindow] = []
    for key, raw in snapshots.items():
        quota = as_record(raw)
        if quota is None:
            raise UsageError(f"Copilot-Quote {key} ist ungültig")
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
        raise UsageError(f"Netzwerk-Timeout nach {timeout:g} s") from None
    except URLError:
        raise UsageError("Netzwerkfehler") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise UsageError("ungültige JSON-Antwort") from None


def read_codex_access(home: Path, label: str) -> tuple[str, str]:
    try:
        auth = as_record(json.loads((home / "auth.json").read_text(encoding="utf-8")))
    except FileNotFoundError:
        raise UsageError(f"{label}-Anmeldung fehlt") from None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise UsageError(f"{label}-Anmeldung kann nicht gelesen werden") from None

    tokens = as_record(auth.get("tokens") if auth else None)
    access_token = tokens.get("access_token") if tokens else None
    account_id = tokens.get("account_id") if tokens else None
    if not isinstance(access_token, str) or not access_token:
        raise UsageError(f"{label}-Anmeldung fehlt")
    if not isinstance(account_id, str) or not account_id:
        raise UsageError(f"{label}-Anmeldung enthält keine Account-ID")
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
        raise UsageError("GitHub CLI `gh` nicht gefunden") from None
    except subprocess.TimeoutExpired:
        raise UsageError("GitHub-CLI-Anmeldung antwortet nicht") from None
    except OSError:
        raise UsageError("GitHub-CLI-Anmeldung konnte nicht gelesen werden") from None
    if completed.returncode != 0 or not completed.stdout.strip():
        raise UsageError("GitHub-CLI-Anmeldung fehlt")
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
    configured = os.environ.get(name) or os.environ.get(f"KI_STATUS_{name}")
    return Path(configured).expanduser() if configured else Path.home() / default_name


def german_number(value: float | None) -> str:
    if value is None:
        return "–"
    rounded = round(value, 1)
    raw = str(int(rounded)) if rounded.is_integer() else f"{rounded:.1f}"
    whole, _, fraction = raw.partition(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    return f"{grouped},{fraction}" if fraction else grouped


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
        countdown = "Reset ist fällig"
    else:
        days, rest = divmod(remaining, 86_400)
        hours, rest = divmod(rest, 3_600)
        minutes, seconds = divmod(rest, 60)
        if days:
            countdown = f"Noch {days} {'Tag' if days == 1 else 'Tage'} · {hours} Std."
        elif hours:
            countdown = f"Noch {hours} Std. · {minutes} Min."
        else:
            countdown = f"Noch {minutes} Min. · {seconds} Sek."
    return countdown


def progress_bar(percent: float) -> str:
    width = 20
    filled = round(clamp_percent(percent) / 100.0 * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def format_window(window: UsageWindow, now: datetime) -> list[str]:
    if window.unlimited:
        return [f"  {window.label:<24} Unbegrenzt"]

    line = f"  {window.label:<24} {german_number(window.used)} / {german_number(window.limit)} {window.unit}"
    if window.used_percent is not None:
        severity = " · KRITISCH" if window.used_percent >= 95 else " · WARNUNG" if window.used_percent >= 80 else ""
        line += f" {progress_bar(window.used_percent)}{severity}"
    lines = [line]
    reset = reset_text(window.reset_at, now)
    if reset:
        lines[0] += f" · {reset}"
    return lines


def format_provider(result: ProviderResult, now: datetime) -> str:
    lines = [result.label]
    if result.error:
        lines.append(f"  Fehler: {result.error}.")
        if result.key == "copilot":
            lines.append("  Hinweis: Lokale Anmeldung mit `gh auth login` prüfen.")
        else:
            lines.append(f"  Hinweis: Lokale Anmeldung mit `{result.key} login` prüfen.")
    else:
        windows = result.windows if result.key in {"codex1", "codex2"} else tuple(
            window for window in result.windows if window.id == "premium_interactions"
        )
        if not windows:
            lines.append("  Keine aktiven Limits gemeldet.")
        else:
            for window in windows:
                lines.extend(format_window(window, now))
    return "\n".join(lines)


def poll_and_print(codex1_home: Path, codex2_home: Path, timeout: float, interval: float) -> None:
    now = datetime.now().astimezone()
    print(f"\nKI-Limits · {now:%d.%m.%Y %H:%M:%S %Z}")
    print(f"Status aktualisiert · nächste Abfrage in {interval:g} s · drei Requests parallel …", flush=True)

    jobs = {
        "copilot": ("GitHub Copilot", lambda: collect_copilot(timeout)),
        "codex1": ("OpenAI Codex 1", lambda: collect_codex("codex1", "Codex 1", codex1_home, timeout)),
        "codex2": ("OpenAI Codex 2", lambda: collect_codex("codex2", "Codex 2", codex2_home, timeout)),
    }
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(job): key for key, (_, job) in jobs.items()}
        results: dict[str, ProviderResult] = {}
        for future in as_completed(futures):
            key = futures[future]
            label = jobs[key][0]
            try:
                result = future.result()
            except Exception:
                result = ProviderResult(key, label, (), "", "unerwarteter Fehler bei der Abfrage")
            results[key] = result

    # Keep the output stable even though the HTTP calls finish at different
    # times.  That makes repeated 30-second snapshots easy to scan.
    for key in ("codex1", "codex2", "copilot"):
        if key in results:
            print(format_provider(results[key], now))
            print()
    sys.stdout.flush()


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("muss größer als 0 sein")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fragt Copilot, Codex 1 und Codex 2 direkt nach ihren Usage-Limits."
    )
    parser.add_argument(
        "--interval",
        type=positive_float,
        default=DEFAULT_INTERVAL,
        metavar="SEKUNDEN",
        help=f"Pause zwischen Abfragen (Standard: {DEFAULT_INTERVAL:g})",
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT,
        metavar="SEKUNDEN",
        help=f"Timeout pro HTTP-/Login-Abfrage (Standard: {DEFAULT_TIMEOUT:g})",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Nur einmal abfragen und danach beenden",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    codex1_home = profile_home("CODEX1_HOME", ".codex-account1")
    codex2_home = profile_home("CODEX2_HOME", ".codex-account2")

    print("AI-Statusmonitor gestartet · Ctrl-C zum Beenden", flush=True)
    try:
        while True:
            started = time.monotonic()
            poll_and_print(codex1_home, codex2_home, args.timeout, args.interval)
            if args.once:
                return 0
            remaining = args.interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    except KeyboardInterrupt:
        print("\nBeendet.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
