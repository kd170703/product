"""Offline adapters tests. Generated fixtures are synthetic, never live statistics."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from monitoring.services.adapters import AdapterError, Download, discover, parse_file, validate_url


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = {"indicator_code": "test", "unit": "чел.", "territory": "Москва"}

    def json_file(self, rows):
        p = self.root / "rows.json"
        p.write_text(json.dumps(rows), encoding="utf-8")
        return p

    def row(self, value, **kwargs):
        return {"value": value, "period_start": "2025-01-01", "period_end": "2025-12-31", "period_kind": "flow", **kwargs}

    def test_zero_missing_and_not_applicable_are_distinct(self):
        p = self.json_file([self.row(0, group="zero"), self.row(None, group="missing"), self.row("не применимо", group="na")])
        records = parse_file(p, "json", self.config)
        self.assertEqual([(r["value"], r["data_state"]) for r in records], [("0", "value"), (None, "missing"), (None, "na")])

    def test_decimal_comma_and_precision(self):
        p = self.json_file([self.row("1 234,567890123")])
        self.assertEqual(parse_file(p, "json", self.config)[0]["value"], "1234.567890123")

    def test_duplicate_dimensions_rejected_even_different_values(self):
        p = self.json_file([self.row(1), self.row(2)])
        with self.assertRaisesRegex(AdapterError, "Дубли"):
            parse_file(p, "json", self.config)

    def test_different_territories_cannot_pass_source_filter(self):
        p = self.json_file([self.row(1, territory="Санкт-Петербург")])
        with self.assertRaisesRegex(AdapterError, "Территория"):
            parse_file(p, "json", self.config)

    def test_different_units_rejected(self):
        p = self.json_file([self.row(1, unit="руб.")])
        with self.assertRaisesRegex(AdapterError, "Единица"):
            parse_file(p, "json", self.config)

    def test_period_required_not_replaced_by_import_date(self):
        p = self.json_file([{"value": 1}])
        with self.assertRaisesRegex(AdapterError, "период"):
            parse_file(p, "json", self.config)

    def test_reversed_period_rejected(self):
        p = self.json_file([self.row(1, period_start="2026-01-01")])
        with self.assertRaisesRegex(AdapterError, "Начало"):
            parse_file(p, "json", self.config)

    def test_annual_and_quarterly_observations_kept_separate(self):
        p = self.json_file([self.row(10), self.row(15, period_start="2025-10-01")])
        records = parse_file(p, "json", self.config)
        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["period_start"], records[1]["period_start"])

    def test_correct_answer_counts_not_guessed(self):
        p = self.json_file([self.row("33.3")])
        r = parse_file(p, "json", self.config)[0]
        self.assertIsNone(r["numerator"])
        self.assertIsNone(r["denominator"])

    def test_invalid_ratio_rejected(self):
        p = self.json_file([self.row(110, numerator=11, denominator=10)])
        with self.assertRaisesRegex(AdapterError, "Числитель"):
            parse_file(p, "json", self.config)

    def test_block_local_and_non_web_addresses(self):
        for url in ["file:///etc/passwd", "http://localhost/a", "http://127.0.0.1/a", "http://[::1]/", "http://169.254.169.254/", "http://user:pass@cbr.ru/", "http://cbr.ru:5432/"]:
            with self.subTest(url=url), self.assertRaises(AdapterError):
                validate_url(url)

    def test_reject_host_resolving_to_private_ip(self):
        with patch("monitoring.services.adapters.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(AdapterError):
                validate_url("https://public-looking.example/")

    def test_cards_discovery_does_not_mix_other_table_six(self):
        html = b'<div class="document-regular">OTHER TABLE<a href="/Collection/T6_2022.xlsx">2022</a></div>' + '<div class="document-regular">Количество расчетных и кредитных карт в территориальном разрезе<a href="/Collection/t6_2026.xlsx">2026</a></div>'.encode()
        response = Download(html, "https://www.cbr.ru/statistics/nps/psrf/", "text/html", "test")
        with patch("monitoring.services.adapters.fetch_url", return_value=response):
            results = discover({"adapter": "cbr_cards", "publications_url": response.url, "mapping": {}})
        self.assertEqual([r["url"] for r in results], ["https://www.cbr.ru/Collection/t6_2026.xlsx"])

    def test_cards_definition_must_be_explicitly_confirmed(self):
        p = self.root / "cards.xlsx"
        Workbook().save(p)
        with self.assertRaisesRegex(AdapterError, "не согласовано"):
            parse_file(p, "cbr_cards", {**self.config, "unit": "тыс. ед."})

    def test_cards_verified_columns_and_stock_date(self):
        p = self.root / "cards.xlsx"
        w = Workbook(); s = w.active; s.title = "на 01.04.2026"
        for cell, value in {"A1": "Количество расчетных и кредитных карт (в территориальном разрезе)", "A2": "на 01.04.2026", "G3": "тыс. ед.", "C5": "всего", "D6": "расчетные", "E6": "кредитные", "B9": "г. Москва", "C9": 15, "D9": 12, "E9": 3}.items():
            s[cell] = value
        w.save(p)
        cfg = {**self.config, "unit": "тыс. ед.", "definition_confirmed": True, "metric": "credit"}
        r = parse_file(p, "cbr_cards", cfg)[0]
        self.assertEqual((r["value"], r["period_kind"], r["period_start"], r["provenance"]["cell"]), ("3", "stock", "2026-04-01", "E9"))

    def test_deposits_dates_from_header_not_job_date(self):
        p = self.root / "deposits.xlsx"
        w = Workbook(); s = w.active; s.title = "итого"
        for cell, value in {"A1": "Вклады физических лиц (без учета счетов эскроу), млн руб.", "B2": "01.01.2025", "C2": "01.02.2025", "A3": "г. Москва", "B3": 0, "C3": 4}.items():
            s[cell] = value
        w.save(p)
        records = parse_file(p, "cbr_deposits", {**self.config, "unit": "млн руб."})
        self.assertEqual([(r["period_start"], r["value"]) for r in records], [("2025-01-01", "0"), ("2025-02-01", "4")])

    def test_unchecked_fns_file_requires_confirmation(self):
        p = self.root / "fns.xlsx"; Workbook().save(p)
        with self.assertRaisesRegex(AdapterError, "не проверена"):
            parse_file(p, "fns_social", self.config)

    def test_saved_official_file_mapping_preserves_period_and_cell(self):
        # Synthetic structure only, not a claim about an actual FNS workbook.
        p = self.root / "mapped.xlsx"
        w = Workbook(); s = w.active; s.title = "Отчет"; s["B4"] = "Социальные вычеты, человек"; s["C4"] = 7; w.save(p)
        cfg = {**self.config, "structure_confirmed": True, "cells": [{"sheet": "Отчет", "value_cell": "C4", "header_cell": "B4", "expected_header": "Социальные вычеты", "period_start": "2025-01-01", "period_end": "2025-12-31", "period_kind": "flow", "period_label": "2025 год"}]}
        r = parse_file(p, "fns_social", cfg)[0]
        self.assertEqual((r["value"], r["period_label"], r["provenance"]["cell"]), ("7", "2025 год", "C4"))

    def test_structure_drift_rejected_before_import(self):
        p = self.root / "mapped.xlsx"
        w = Workbook(); s = w.active; s.title = "Отчет"; s["B4"] = "Сумма, рублей"; s["C4"] = 7; w.save(p)
        cfg = {**self.config, "cells": [{"sheet": "Отчет", "value_cell": "C4", "header_cell": "B4", "expected_header": "Человек", "period_start": "2025-01-01"}]}
        with self.assertRaisesRegex(AdapterError, "Изменился заголовок"):
            parse_file(p, "xlsx", cfg)


if __name__ == "__main__":
    unittest.main()
