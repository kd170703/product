"""Calendar regression cases use synthetic dates and labels only."""
import unittest
from datetime import date
from types import SimpleNamespace
from monitoring.services.periods import classify_period, matches_period, normalize_period_filters, period_label_for_filters, period_sort_key, quarter_bounds


class PeriodTests(unittest.TestCase):
    def quarter(self, year=2024, quarter=1):
        return {"period_mode": "quarter", "period_year": str(year), "period_quarter": str(quarter)}

    def test_quarter_input_spellings(self):
        for text in ["1 квартал 2024 года", "I кв. 2024", "Q1 2024", "2024-Q1", "2024 год 1 квартал", "Ⅰ квартал 2024"]:
            with self.subTest(text=text):
                f=normalize_period_filters({"period":text})
                self.assertEqual(f["period_mode"],"quarter")
                self.assertEqual(f["period_year"],"2024")
                self.assertEqual(f["period_quarter"],"1")

    def test_complete_search_period_is_consumed_but_business_search_is_not(self):
        f=normalize_period_filters({"q":"1 квартал 2024 года","topic":"tax"})
        self.assertNotIn("q",f)
        self.assertEqual(f["period_year"],"2024")
        self.assertEqual(f["topic"],"tax")
        self.assertEqual(normalize_period_filters({"q":"вклады за I квартал 2024"})["q"],"вклады за I квартал 2024")

    def test_aliases_and_roman_quarter_controls(self):
        f=normalize_period_filters({"year":"2025","quarter":"IV"})
        self.assertEqual((f["period_mode"],f["period_year"],f["period_quarter"]),("quarter","2025","4"))

    def test_custom_dates_do_not_override_selected_quarter(self):
        f=normalize_period_filters({**self.quarter(),"start":"2025-01-01","end":"2025-03-31"})
        self.assertNotIn("start",f)
        self.assertNotIn("end",f)
        self.assertTrue(matches_period({"period_label":"I квартал 2024"},f))

    def test_quarter_boundaries_include_leap_year(self):
        self.assertEqual(quarter_bounds(2024,1),(date(2024,1,1),date(2024,3,31)))
        self.assertEqual(quarter_bounds(2024,4),(date(2024,10,1),date(2024,12,31)))
        with self.assertRaises(ValueError):quarter_bounds(2024,5)

    def test_raw_quarter_with_no_dates_is_selected(self):
        r={"period_label":"I кв. 2024","period_kind":"unknown","period_start":None,"period_end":None}
        self.assertTrue(matches_period(r,self.quarter()))
        self.assertFalse(matches_period(r,self.quarter(2025,1)))
        self.assertFalse(matches_period(r,self.quarter(2024,4)))
        self.assertIsNone(r["period_start"])

    def test_mixed_quarter_year_keeps_qualification_and_original_label(self):
        r={"period_label":"IV кв. 2024 / 2024","period_kind":"unknown"}
        p=classify_period(r)
        self.assertEqual((p["year"],p["quarter"],p["kind"]),(2024,4,"mixed"))
        self.assertTrue(p["qualification"])
        self.assertTrue(matches_period(r,self.quarter(2024,4)))
        self.assertTrue(matches_period(r,{"period_mode":"year","period_year":"2024"}))
        self.assertIsNone(p["start"])
        self.assertEqual(r["period_label"],"IV кв. 2024 / 2024")

    def test_annual_is_not_a_fourth_quarter(self):
        for r in [{"period_label":"2024 год"},{"period_label":"2024","period_start":"2024-01-01","period_end":"2024-12-31","period_kind":"flow"}]:
            self.assertFalse(matches_period(r,self.quarter(2024,4)))
            self.assertTrue(matches_period(r,{"period_mode":"year","period_year":"2024"}))

    def test_halfyear_is_not_first_quarter(self):
        for r in [{"period_label":"I полугодие 2026"},{"period_label":"Первое полугодие","period_start":"2026-01-01","period_end":"2026-06-30"}]:
            self.assertFalse(matches_period(r,self.quarter(2026,1)))
            self.assertTrue(matches_period(r,{"period_mode":"year","period_year":"2026"}))

    def test_year_filter_includes_contained_sociology_wave(self):
        r={"period_label":"Волна","period_start":"2026-04-30","period_end":"2026-05-25","period_kind":"flow"}
        self.assertTrue(matches_period(r,self.quarter(2026,2)))
        self.assertFalse(matches_period(r,self.quarter(2026,1)))
        self.assertTrue(matches_period(r,{"period_mode":"year","period_year":"2026"}))

    def test_overlapping_multi_quarter_window_not_relabelled_as_quarter(self):
        r={"period_label":"Август — декабрь 2024","period_start":"2024-08-01","period_end":"2024-12-31","period_kind":"flow"}
        self.assertFalse(matches_period(r,self.quarter(2024,4)))
        self.assertTrue(matches_period(r,{"period_mode":"year","period_year":"2024"}))

    def test_stock_january_first_is_previous_year_end(self):
        r={"period_label":"01.01.2025","period_start":"2025-01-01","period_end":"2025-01-01","period_kind":"stock"}
        self.assertTrue(matches_period(r,self.quarter(2024,4)))
        self.assertFalse(matches_period(r,self.quarter(2025,1)))
        self.assertTrue(matches_period(r,{"period_mode":"year","period_year":"2024"}))
        self.assertTrue(matches_period(r,{"period_mode":"date","start":"2025-01-01","end":"2025-01-01"}))
        self.assertFalse(matches_period(r,{"period_mode":"date","start":"2024-01-01","end":"2024-12-31"}))

    def test_stock_april_first_closes_first_quarter_without_mutation(self):
        r=SimpleNamespace(period_label="на 01.04.2026",period_start=date(2026,4,1),period_end=date(2026,4,1),period_kind="stock")
        self.assertTrue(matches_period(r,self.quarter(2026,1)))
        self.assertFalse(matches_period(r,self.quarter(2026,2)))
        self.assertEqual(r.period_end,date(2026,4,1))
        self.assertTrue(classify_period(r)["qualification"])

    def test_dated_flow_on_quarter_first_day_is_not_shifted_back(self):
        r={"period_label":"Дневное значение","period_start":"2025-01-01","period_end":"2025-01-01","period_kind":"flow"}
        self.assertTrue(matches_period(r,self.quarter(2025,1)))
        self.assertFalse(matches_period(r,self.quarter(2024,4)))

    def test_date_mode_preserves_full_containment(self):
        r={"period_label":"2025 год","period_start":"2025-01-01","period_end":"2025-12-31","period_kind":"flow"}
        self.assertFalse(matches_period(r,{"start":"2025-01-01","end":"2025-03-31"}))
        self.assertTrue(matches_period(r,{"start":"2025-01-01","end":"2025-12-31"}))
        self.assertFalse(matches_period({"period_label":"I кв.2025"},{"start":"2025-01-01","end":"2025-03-31"}))

    def test_unknown_and_cross_year_periods_do_not_gain_calendar_year(self):
        self.assertFalse(matches_period({"period_label":"Неизвестно"},self.quarter()))
        self.assertFalse(matches_period({"period_label":"Длинная волна","period_start":"2024-12-01","period_end":"2025-01-15"},{"period_mode":"year","period_year":"2025"}))

    def test_invalid_filters_fail_closed_without_exception(self):
        for f in [{"period_mode":"quarter","period_year":"2024","period_quarter":"5"},{"period_mode":"quarter","period_year":"bad","period_quarter":"1"},{"start":"2025-07-01","end":"2025-01-01"},{"start":"1 квартал 2024"}]:
            self.assertIn("_period_error",normalize_period_filters(f))
            self.assertFalse(matches_period({"period_label":"I кв2024"},f))

    def test_sorting_uses_calendar_order_not_roman_or_lexical_order(self):
        labels=["I кв. 2025","IV кв. 2024 / 2024","II кв.2024","I кв.2024"]
        actual=sorted(labels,key=lambda s:period_sort_key({"period_label":s}))
        self.assertEqual(actual,["I кв.2024","II кв.2024","IV кв. 2024 / 2024","I кв. 2025"])

    def test_human_labels_for_all_filter_modes(self):
        self.assertEqual(period_label_for_filters(self.quarter()),"I квартал 2024 года")
        self.assertEqual(period_label_for_filters({"period_mode":"year","period_year":"2026"}),"2026 год")
        self.assertEqual(period_label_for_filters({"start":"2024-01-01","end":"2024-03-31"}),"01.01.2024 — 31.03.2024")
        self.assertEqual(period_label_for_filters({}),"Все периоды")


if __name__ == '__main__':
    unittest.main()


class AdditionalQuarterRegressions(unittest.TestCase):
    def test_stock_with_annual_caption_uses_real_closing_date(self):
        row={"period_label":"2024 год","period_start":"2025-01-01","period_end":"2025-01-01","period_kind":"stock"}
        self.assertTrue(matches_period(row,{"period_year":"2024","period_quarter":"4"}))
        self.assertFalse(matches_period(row,{"period_year":"2025","period_quarter":"1"}))
    def test_explicit_all_ignores_previous_quarter_fields(self):
        filters=normalize_period_filters({"period_mode":"all","period_year":"2024","period_quarter":"1"})
        self.assertEqual(filters["period_mode"],"all")
        self.assertTrue(matches_period({"period_label":"2025 год"},filters))
    def test_invalid_period_stays_invalid_after_double_normalization(self):
        filters=normalize_period_filters({"period_mode":"invalid"})
        self.assertFalse(matches_period({"period_label":"2025 год"},normalize_period_filters(filters)))
