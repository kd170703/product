"""Database/storage check for immutable released report contents."""
import hashlib
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from monitoring.models import Formula, Indicator, Methodology, ReportSnapshot
from monitoring.services.exports import generate_report


class ReportPersistenceTests(TestCase):
    def test_new_values_and_methodology_edits_do_not_rewrite_released_files(self):
        user = get_user_model().objects.create_user("report-author", password="unused-in-test")
        indicator = Indicator.objects.create(code="REPORT-TEST", name="Тестовый показатель", section="statistics", unit="руб.")
        method = Methodology.objects.create(code="REPORT-METHOD", version=1, name="Методика отчёта", approved=False, description="Исходная версия")
        formula = Formula.objects.create(indicator=indicator, methodology=method, expression={"operation": "sum", "components": ["A", "B"]})
        rows = [{"id": 1, "indicator_code": indicator.code, "indicator_name": indicator.name, "section": "statistics", "topics": ["Сбережения"], "value": "10.5", "data_state": "value", "unit": "руб.", "territory": "Москва", "period_label": "I квартал 2025", "period_start": "2025-01-01", "period_end": "2025-03-31", "period_kind": "flow", "observation_version_id": 901, "method_version_id": method.pk, "status": "verified"}]
        with tempfile.TemporaryDirectory() as directory:
            with override_settings(MEDIA_ROOT=directory):
                report = generate_report(rows, {"period": "I квартал 2025", "territory": "Москва"}, user)
                self.assertIsInstance(report, ReportSnapshot)
                self.assertEqual(report.period_label, "I квартал 2025")
                self.assertEqual(report.territory, "Москва")
                original_checksum = report.snapshot["checksum"]
                original_docx = Path(report.docx_file.path).read_bytes()
                original_xlsx = Path(report.xlsx_file.path).read_bytes()
                self.assertTrue(original_docx.startswith(b"PK"))
                self.assertTrue(original_xlsx.startswith(b"PK"))
                self.assertEqual(report.snapshot["data_version_ids"], ["901"])
                self.assertFalse(report.snapshot["method_versions"][0]["approved"])
                self.assertEqual(report.snapshot["method_versions"][0]["formulas"][0]["expression"]["components"], ["A", "B"])
                rows[0].update(value="99.0", observation_version_id=902)
                method.description = "Изменённая методика"
                method.approved = True
                method.save()
                formula.expression = {"operation": "sum", "components": ["A", "C"]}
                formula.save()
                report.refresh_from_db()
                self.assertEqual(report.snapshot["checksum"], original_checksum)
                self.assertEqual(report.snapshot["rows"][0]["value"], "10.5")
                self.assertEqual(report.snapshot["method_versions"][0]["description"], "Исходная версия")
                self.assertFalse(report.snapshot["method_versions"][0]["approved"])
                self.assertEqual(hashlib.sha256(Path(report.docx_file.path).read_bytes()).digest(), hashlib.sha256(original_docx).digest())
                self.assertEqual(hashlib.sha256(Path(report.xlsx_file.path).read_bytes()).digest(), hashlib.sha256(original_xlsx).digest())
