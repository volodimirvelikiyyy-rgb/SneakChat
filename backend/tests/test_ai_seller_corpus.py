"""Data-driven deterministic intent corpus. LLM evaluation is intentionally separate."""

import json
import importlib
import unittest
from pathlib import Path

CORPUS_PATH = Path(__file__).parent / "data" / "ai_seller_cases.jsonl"


def load_cases():
    cases = [json.loads(line) for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = [case["id"] for case in cases]
    inputs = [case["input"].casefold() for case in cases]
    if len(ids) != len(set(ids)) or len(inputs) != len(set(inputs)):
        raise ValueError("AI seller corpus IDs and inputs must be unique")
    return cases


class AiSellerCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_cases()

    def test_corpus_is_large_and_has_required_contract_fields(self):
        parser_cases = [case for case in self.cases if case["execution"] == "parser"]
        gate_cases = [case for case in self.cases if case["execution"] == "gate"]
        self.assertGreaterEqual(len(parser_cases), 150)
        self.assertGreaterEqual(len(gate_cases), 30)
        self.assertGreaterEqual(len({case["category"] for case in parser_cases}), 7)
        required = {"id", "category", "execution", "input", "expected_filters", "invariants",
                    "success_criteria", "error_category"}
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(required.issubset(case))
                self.assertTrue(case["input"].strip())

    def test_new_scenarios_have_verifiable_contracts_and_controlled_fixture(self):
        contracts = [case for case in self.cases if case["execution"] == "contract"]
        self.assertEqual(len(contracts), 20)
        expected_counts = {"sku_lookup": 3, "two_model_comparison": 2,
                           "fit_and_width": 2, "buyer_objection": 3,
                           "adaptive_preference": 3, "exact_model_lock": 1,
                           "adaptive_style": 1, "adaptive_purpose": 1,
                           "adaptive_premium": 1, "adaptive_collection": 1,
                           "adaptive_open_recommendation": 1, "hard_budget": 1}
        self.assertEqual({category: sum(case["category"] == category for case in contracts)
                          for category in expected_counts}, expected_counts)
        for case in contracts:
            with self.subTest(case=case["id"]):
                self.assertEqual(case["fixture"], "ai_seller_core")
                self.assertIn(case["status"], {"PASS", "FAIL", "BLOCKED", "SPEC ONLY",
                                                "pending_implementation", "pending_catalog_data"})
                if case["status"] in {"PASS", "FAIL", "BLOCKED"}:
                    self.assertTrue(case.get("automated_test"))
                self.assertTrue(case.get("expected_result"))
                self.assertGreaterEqual(len(case["invariants"]), 2)
                self.assertGreaterEqual(len(case["success_criteria"]), 30)

    def test_contract_automated_test_references_resolve(self):
        for case in self.cases:
            if case["execution"] != "contract" or "automated_test" not in case:
                continue
            with self.subTest(case=case["id"]):
                parts = case["automated_test"].split(".")
                test_class = getattr(importlib.import_module(".".join(parts[:-2])), parts[-2])
                self.assertTrue(callable(getattr(test_class, parts[-1])))


if __name__ == "__main__":
    unittest.main()
