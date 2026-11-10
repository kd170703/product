"""Calendar-aware period filters without changing published observation dates.

A raw label such as ``IV кв. 2024 / 2024`` retains its ambiguity.  A quarter
filter may find its explicitly stated quarter, but classification remains mixed.
Stock dates on the first day of a quarter are closing balances of the previous
quarter; custom date filters continue to use the actual observation date.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta

ROMAN_QUARTERS = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
QUARTER_NAMES = ("I", "II", "III", "IV")
_YEAR = r"(?:19|20|21)\d{2}"
_Q = r"(?:iv|iii|ii|i|[1-4])"
_QUARTER_WORD = r"(?:квартал(?:а|у|ом|е)?|кв\.?)"
_QUARTER_PATTERNS = [
    re.compile(rf"(?<![\w])(?P<quarter>{_Q})\s*(?:[-–]?\s*(?:й|ый|ой))?\s*{_QUARTER_WORD}\s*(?:за\s*)?(?P<year>{_YEAR})(?!\d)", re.I),
    re.compile(rf"(?<![\w])q\s*(?P<quarter>[1-4])\s*[,./–-]?\s*(?P<year>{_YEAR})(?!\d)", re.I),
    re.compile(rf"(?<!\d)(?P<year>{_YEAR})\s*(?:г(?:од(?:а)?|\.)?\s*)?[,/–-]?\s*(?P<quarter>{_Q})\s*{_QUARTER_WORD}(?!\w)", re.I),
    re.compile(rf"(?<!\d)(?P<year>{_YEAR})\s*[,/–-]?\s*q\s*(?P<quarter>[1-4])(?!\d)", re.I),
]
_HALF_YEAR = re.compile(rf"(?<!\w)(?P<half>ii|i|[12])\s*(?:полугод(?:ие|ия|ии)|полуг\.?|п\.?\s*г\.?)\s*(?P<year>{_YEAR})(?!\d)", re.I)
_ANNUAL = re.compile(rf"^(?:за\s*)?(?P<year>{_YEAR})(?:\s*г(?:од(?:а)?|\.)?)?[\s.]*$", re.I)
_DATE = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4})(?!\d)")


def _get(record, key, default=None):
    return record.get(key, default) if isinstance(record, dict) else getattr(record, key, default)


def _text(value):
    if isinstance(value, (list, tuple)):
        value = value[-1] if value else ""
    return str(value or "").strip()


def _normalize_label(value):
    return re.sub(r"\s+", " ", _text(value).translate(str.maketrans({"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV"})).replace("ё", "е").replace("\xa0", " ")).strip()


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(str(value)[:10], fmt).date()
        except (ValueError, TypeError):
            pass
    return None


def quarter_bounds(year, quarter):
    """Return inclusive calendar boundaries, never observation replacements."""
    year, quarter = int(year), int(quarter)
    if not 1 <= quarter <= 4:
        raise ValueError("Квартал должен быть от 1 до 4.")
    month = (quarter - 1) * 3 + 1
    last_month = month + 2
    return date(year, month, 1), date(year, last_month, calendar.monthrange(year, last_month)[1])


def _quarter_in_label(label):
    for pattern in _QUARTER_PATTERNS:
        match = pattern.search(label)
        if match:
            token = match["quarter"].lower()
            quarter = ROMAN_QUARTERS[token] if token in ROMAN_QUARTERS else int(token)
            return int(match["year"]), quarter, match
    return None


def _period_only(label):
    # Only consume a search query when its complete content is a period, never
    # remove business words from a query such as "вклады за I квартал 2024".
    residue = re.sub(r"\b(?:за|год|года|г|квартал|квартала|кв|q[1-4]|iv|iii|ii|i)\b|[0-9\s,./–—-]", "", label.lower())
    return not residue.strip()


def normalize_period_filters(filters):
    """Return a copied, canonical filter dictionary.

    Canonical inputs: period_mode=quarter/year/date/all, period_year and
    period_quarter.  Aliases year/quarter and a complete period or q string are
    accepted.  Invalid selections set _period_error and match no observations.
    Unrelated filters are preserved, and quarter/year modes ignore custom dates.
    """
    result = {key: _text(value) for key, value in dict(filters or {}).items() if value is not None}
    mode = result.get("period_mode", "").lower()
    year = result.get("period_year") or result.get("year", "")
    quarter = result.get("period_quarter") or result.get("quarter", "")
    previous_error=result.pop("_period_error", None)
    if mode=="all":
        year=quarter=""
        for key in ("period_year","period_quarter","year","quarter","start","end"):result.pop(key,None)
    period_text = _normalize_label(result.get("period", ""))
    if not period_text and not year and not quarter and mode in {"", "all"}:
        query = _normalize_label(result.get("q", ""))
        if _period_only(query) and (_quarter_in_label(query) or _ANNUAL.fullmatch(query)):
            period_text = query
            result.pop("q", None)
    if period_text and mode not in {"quarter", "year", "date"}:
        parsed = _quarter_in_label(period_text)
        annual = _ANNUAL.fullmatch(period_text)
        if parsed:
            year, quarter, _ = parsed
            mode = "quarter"
        elif annual:
            year, mode = annual["year"], "year"
        elif period_text.lower() not in {"все", "все периоды", "all"}:
            result["_period_error"] = "Период не распознан. Выберите год и квартал или задайте диапазон дат."
    if not mode or mode == "all":
        mode = "quarter" if quarter else "year" if year else "date" if result.get("start") or result.get("end") else "all"
    if mode not in {"quarter", "year", "date", "all"}:
        result["_period_error"] = "Неизвестный способ выбора периода."
        mode = "all"
    if mode in {"quarter", "year"}:
        try:
            numeric_year = int(year)
            if not 1900 <= numeric_year <= 2199:
                raise ValueError()
            result["period_year"] = str(numeric_year)
        except (ValueError, TypeError):
            result["_period_error"] = "Выберите корректный год."
        if mode == "quarter":
            token = _text(quarter).lower()
            try:
                numeric_quarter = ROMAN_QUARTERS.get(token) or int(token)
                if numeric_quarter not in range(1, 5):
                    raise ValueError()
                result["period_quarter"] = str(numeric_quarter)
            except (ValueError, TypeError):
                result["_period_error"] = "Выберите квартал от I до IV."
        else:
            result.pop("period_quarter", None)
        result.pop("start", None)
        result.pop("end", None)
    elif mode == "date":
        for key in ("start", "end"):
            if result.get(key) and _date(result[key]) is None:
                result["_period_error"] = "Укажите даты в формате ГГГГ-ММ-ДД."
        if _date(result.get("start")) and _date(result.get("end")) and _date(result["start"]) > _date(result["end"]):
            result["_period_error"] = "Начало диапазона не может быть позже конца."
    result["period_mode"] = mode
    if previous_error and not result.get("_period_error"):result["_period_error"]=previous_error
    return result


def classify_period(record):
    """Describe an observation's calendar position without mutating it.

    kind is quarter, mixed, year, halfyear, stock, month, day, range, unknown or
    ambiguous.  start/end retain actual source dates (possibly None).  A mixed
    label has a usable declared quarter but no fabricated exact dates.
    """
    label = _normalize_label(_get(record, "period_label", _get(record, "period", "")))
    start, end = _date(_get(record, "period_start")), _date(_get(record, "period_end"))
    actual_start, actual_end = start, end
    if not start and not end:
        label_dates = [_date(m.group()) for m in _DATE.finditer(label)]
        label_dates = [d for d in label_dates if d]
        if len(label_dates) >= 2:
            start, end = label_dates[0], label_dates[1]
        elif len(label_dates) == 1:
            start = end = label_dates[0]
    if start and end and start > end:
        return {"year": None, "quarter": None, "kind": "ambiguous", "label": label, "start": actual_start, "end": actual_end, "sort_key": (999999999, 9, label), "qualification": "Начало исходного периода позже его окончания."}
    kind = _get(record, "period_kind", _get(record, "metric_kind", "unknown"))
    stock = kind == "stock" or bool(re.search(r"(?:^|\s)на\s+\d{1,2}\.\d{1,2}\.\d{4}", label, re.I))
    declared = _quarter_in_label(label)
    half = _HALF_YEAR.search(label)
    annual = _ANNUAL.fullmatch(label)
    year = quarter = None
    classification = "unknown"
    qualification = ""
    anchor = end or start
    if declared:
        year, quarter, match = declared
        suffix = label[match.end():]
        mixed = bool(re.search(rf"\s*/\s*{_YEAR}(?:\s*г(?:од(?:а)?|\.)?)?\s*$", suffix, re.I))
        classification = "mixed" if mixed else "quarter"
        qualification = "Исходная подпись совмещает квартал и год; значение не разделено на два наблюдения." if mixed else ("Квартал определён по подписи источника; точная дата наблюдения не задана." if stock and not anchor else "")
    elif half:
        year, classification = int(half["year"]), "halfyear"
    elif annual and not (stock and anchor):
        year, classification = int(annual["year"]), "year"
    elif stock and anchor:
        # A balance "на 01.04" is the closing balance of Q1, not Q2.
        calendar_date = anchor - timedelta(days=1) if anchor.day == 1 and anchor.month in (1, 4, 7, 10) else anchor
        year, quarter = calendar_date.year, (calendar_date.month - 1) // 3 + 1
        classification = "stock"
        if calendar_date != anchor:
            qualification = "Остаток на первое число следующего квартала относится к окончанию предыдущего; исходная дата сохранена."
    elif start and end:
        same_year = start.year == end.year
        year = start.year if same_year else None
        if same_year and start == date(year, 1, 1) and end == date(year, 12, 31):
            classification = "year"
        elif same_year and (start, end) in ((date(year, 1, 1), date(year, 6, 30)), (date(year, 7, 1), date(year, 12, 31))):
            classification = "halfyear"
        else:
            same_quarter = same_year and (start.month - 1) // 3 == (end.month - 1) // 3
            quarter = (start.month - 1) // 3 + 1 if same_quarter else None
            if start == end:
                classification = "day"
            elif same_year and start.month == end.month and start.day == 1 and end.day == calendar.monthrange(end.year, end.month)[1]:
                classification = "month"
            elif same_quarter and (start, end) == quarter_bounds(year, quarter):
                classification = "quarter"
            else:
                classification = "range"
    elif anchor:
        year, quarter = anchor.year, (anchor.month - 1) // 3 + 1
        classification = "day" if kind != "cumulative" else "range"
    if anchor:
        ordinal = anchor.toordinal()
    elif year and quarter:
        ordinal = quarter_bounds(year, quarter)[1].toordinal()
    elif year:
        ordinal = date(year, 6 if half and half["half"].lower() in {"i", "1"} else 12, 30 if half and half["half"].lower() in {"i", "1"} else 31).toordinal()
    else:
        ordinal = 999999999
    return {"year": year, "quarter": quarter, "kind": classification, "label": label, "start": start, "end": end, "sort_key": (ordinal, {"year": 0, "halfyear": 1, "mixed": 2, "quarter": 3, "stock": 4}.get(classification, 5), label), "qualification": qualification}


def matches_period(record, filters):
    filters = normalize_period_filters(filters)
    if filters.get("_period_error"):
        return False
    mode = filters["period_mode"]
    if mode == "all":
        return True
    period = classify_period(record)
    if mode == "quarter":
        if period["kind"] in {"year", "halfyear", "ambiguous", "unknown"}:
            return False
        return period["year"] == int(filters["period_year"]) and period["quarter"] == int(filters["period_quarter"])
    if mode == "year":
        return period["year"] == int(filters["period_year"])
    start, end = period["start"], period["end"]
    # An incomplete starting date may still represent a dated observation; this
    # mirrors the existing full-containment filter's stock/date behaviour.
    observation_start = start or end
    from_date, to_date = _date(filters.get("start")), _date(filters.get("end"))
    if from_date and (observation_start is None or observation_start < from_date):
        return False
    if to_date and (end is None or end > to_date):
        return False
    return True


def period_sort_key(record):
    return classify_period(record)["sort_key"]


def period_label_for_filters(filters):
    filters = normalize_period_filters(filters)
    if filters.get("_period_error"):
        return "Некорректный период"
    mode = filters["period_mode"]
    if mode == "quarter":
        return f"{QUARTER_NAMES[int(filters['period_quarter']) - 1]} квартал {filters['period_year']} года"
    if mode == "year":
        return f"{filters['period_year']} год"
    if mode == "date":
        start, end = _date(filters.get("start")), _date(filters.get("end"))
        return f"{start.strftime('%d.%m.%Y') if start else 'Начало не задано'} — {end.strftime('%d.%m.%Y') if end else 'Конец не задан'}"
    return "Все периоды"
