"""Daily reference rates from Frankfurter with a persistent last-good cache."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

SUPPORTED_CURRENCIES = {"USD", "EUR", "UAH"}
_API_URL = "https://api.frankfurter.dev/v2/rates?base=EUR&quotes=USD%2CUAH"
_CACHE_PATH = Path(__file__).resolve().parents[3] / "outputs" / "currency_rates.json"
_LOCK = threading.Lock()
_logger = logging.getLogger(__name__)
_LAST_ATTEMPT: datetime | None = None


def _read_cache(path: Path = _CACHE_PATH) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rates = data.get("rates", {})
        if (data.get("base") != "EUR" or not all(code in rates for code in SUPPORTED_CURRENCIES)
                or any(float(rates[code]) <= 0 for code in SUPPORTED_CURRENCIES)):
            return None
        data["rates"] = {code: float(rates[code]) for code in SUPPORTED_CURRENCIES}
        data["rates"]["EUR"] = 1.0
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _write_cache(data: dict, path: Path = _CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
            temp_name = handle.name
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _fetch_rates() -> dict:
    request = Request(_API_URL, headers={"Accept": "application/json", "User-Agent": "SneakChat/1.0"})
    with urlopen(request, timeout=5) as response:
        rows = json.loads(response.read().decode("utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Unexpected Frankfurter response")
    values = {row.get("quote"): float(row["rate"]) for row in rows if row.get("base") == "EUR"}
    if not all(code in values and values[code] > 0 for code in ("USD", "UAH")):
        raise ValueError("Frankfurter did not return all required currency quotes")
    dates = [date.fromisoformat(row["date"]) for row in rows if row.get("date")]
    if not dates:
        raise ValueError("Frankfurter response has no reference date")
    return {
        "base": "EUR",
        "rates": {"EUR": 1.0, "USD": values["USD"], "UAH": values["UAH"]},
        "rate_date": min(dates).isoformat(),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "Frankfurter",
    }


def get_rates(*, cache_path: Path = _CACHE_PATH, fetcher=_fetch_rates, now: datetime | None = None) -> dict:
    """Refresh no more than daily; on errors return only a validated saved snapshot."""
    global _LAST_ATTEMPT
    current = now or datetime.now(timezone.utc)
    cached = _read_cache(cache_path)
    fresh_enough = False
    if cached:
        try:
            fetched_at = datetime.fromisoformat(cached["fetched_at"])
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=timezone.utc)
            fresh_enough = current - fetched_at < timedelta(days=1)
        except (KeyError, TypeError, ValueError):
            pass
    recent_failed_attempt = (
        cache_path == _CACHE_PATH and _LAST_ATTEMPT is not None
        and current - _LAST_ATTEMPT < timedelta(days=1)
    )
    if not fresh_enough and not recent_failed_attempt:
        with _LOCK:
            # Another request may have refreshed while this request waited.
            cached = _read_cache(cache_path)
            if cached:
                try:
                    fetched_at = datetime.fromisoformat(cached["fetched_at"])
                    if fetched_at.tzinfo is None:
                        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
                    fresh_enough = current - fetched_at < timedelta(days=1)
                except (KeyError, TypeError, ValueError):
                    fresh_enough = False
            if not fresh_enough:
                try:
                    _LAST_ATTEMPT = current
                    refreshed = fetcher()
                    if refreshed.get("base") != "EUR" or not all(
                        float(refreshed["rates"][code]) > 0 for code in SUPPORTED_CURRENCIES
                    ) or not refreshed.get("rate_date"):
                        raise ValueError("Invalid currency snapshot")
                    refreshed["rates"] = {code: float(refreshed["rates"][code]) for code in SUPPORTED_CURRENCIES}
                    refreshed["rates"]["EUR"] = 1.0
                    _write_cache(refreshed, cache_path)
                    cached = refreshed
                    fresh_enough = True
                except Exception as error:  # network and malformed responses both fall back to last good data
                    _logger.warning("Frankfurter refresh failed: %s", type(error).__name__)
                    cached = _read_cache(cache_path)
    elif recent_failed_attempt and not fresh_enough:
        # A previous failed attempt is throttled for the day; keep serving only the saved snapshot.
        cached = _read_cache(cache_path)
    if not cached:
        return {"available": False, "rates": None, "rate_date": None, "stale": False, "source": "Frankfurter"}
    return {
        "available": True,
        "base": "EUR",
        "rates": cached["rates"],
        "rate_date": cached.get("rate_date"),
        "stale": not fresh_enough,
        "source": "Frankfurter",
    }


def convert_amount(amount: float, source: str, target: str, rates: dict | None) -> float | None:
    """Convert through EUR and round the displayed amount; no missing-rate estimate."""
    if source not in SUPPORTED_CURRENCIES or target not in SUPPORTED_CURRENCIES:
        return None
    if source == target:
        return round(float(amount) + 1e-10, 2)
    if not rates:
        return None
    source_rate = float(rates.get(source, 0))
    target_rate = float(rates.get(target, 0))
    if source_rate <= 0 or target_rate <= 0:
        return None
    return round((float(amount) / source_rate) * target_rate + 1e-10, 2)

