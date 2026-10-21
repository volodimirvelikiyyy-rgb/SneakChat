import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.services.currency import convert_amount, get_rates


class CurrencyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.cache = Path(self.temp_dir.name) / "rates.json"
        self.now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
        self.snapshot = {
            "base": "EUR",
            "rates": {"EUR": 1.0, "USD": 1.1, "UAH": 44.0},
            "rate_date": "2026-10-10",
            "fetched_at": self.now.isoformat(),
            "source": "Frankfurter",
        }

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_converts_all_pairs_and_rounds_to_two_decimals(self):
        rates = self.snapshot["rates"]
        self.assertEqual(convert_amount(10, "USD", "EUR", rates), 9.09)
        self.assertEqual(convert_amount(10, "EUR", "UAH", rates), 440.0)
        self.assertEqual(convert_amount(10, "UAH", "USD", rates), 0.25)
        self.assertEqual(convert_amount(10.005, "EUR", "EUR", rates), 10.01)

    def test_refreshes_at_most_once_per_day_and_persists_rate_date(self):
        calls = []
        def fetcher():
            calls.append(1)
            return dict(self.snapshot)

        first = get_rates(cache_path=self.cache, fetcher=fetcher, now=self.now)
        later = get_rates(cache_path=self.cache, fetcher=fetcher, now=self.now.replace(hour=22))
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["rate_date"], "2026-10-10")
        self.assertEqual(later["rates"]["UAH"], 44.0)
        self.assertFalse(later["stale"])

    def test_api_failure_uses_only_saved_snapshot_and_marks_it_stale(self):
        get_rates(cache_path=self.cache, fetcher=lambda: dict(self.snapshot), now=self.now)
        stale = get_rates(
            cache_path=self.cache,
            fetcher=lambda: (_ for _ in ()).throw(OSError("offline")),
            now=self.now.replace(day=13),
        )
        self.assertTrue(stale["available"])
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["rate_date"], "2026-10-10")
        self.assertEqual(stale["rates"]["UAH"], 44.0)

    def test_without_a_saved_rate_conversion_is_unavailable(self):
        result = get_rates(
            cache_path=self.cache,
            fetcher=lambda: (_ for _ in ()).throw(OSError("offline")),
            now=self.now,
        )
        self.assertFalse(result["available"])
        self.assertIsNone(result["rates"])
        self.assertIsNone(convert_amount(100, "USD", "UAH", result["rates"]))


if __name__ == "__main__":
    unittest.main()
