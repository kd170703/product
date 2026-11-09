"""Bounded downloads and explicit, reproducible statistical file adapters.

No Django dependency. Parsing a saved file uses exactly the network import parser.
Dates here are observation dates/periods, never the day a job happens to run.
"""
from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bs4 import BeautifulSoup
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string

MAX_BYTES = 25 * 1024 * 1024
MAX_ROWS = 50000
MAX_UNCOMPRESSED = 120 * 1024 * 1024


class AdapterError(ValueError):
    """Actionable source/structure error, safe to display in a job log."""


@dataclass(frozen=True)
class Download:
    content: bytes
    url: str
    content_type: str
    checksum: str
    etag: str = ""
    last_modified: str = ""


def validate_url(url):
    """Refuse credentials, non-web schemes, unsafe ports and private addresses."""
    try:
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").rstrip(".").lower()
        port = p.port
    except ValueError as exc:
        raise AdapterError("Некорректный адрес источника") from exc
    if p.scheme not in {"http", "https"} or not host or p.username or p.password:
        raise AdapterError("Разрешены только HTTP(S)-адреса без пароля")
    if port and port not in {80, 443}:
        raise AdapterError("Разрешены только порты 80 и 443")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise AdapterError("Адрес внутренней сети запрещён")
    try:
        addresses = {a[4][0] for a in socket.getaddrinfo(host, port or (443 if p.scheme == "https" else 80))}
    except OSError as exc:
        raise AdapterError(f"Не удалось определить адрес сервера {host}") from exc
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise AdapterError("Адрес внутренней, служебной или частной сети запрещён")
    return url


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_url(url, max_bytes=MAX_BYTES, timeout=25):
    """Use inherited proxy/CA configuration; every redirect is validated."""
    validate_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": "FinancialCultureMonitor/0.1", "Accept-Encoding": "identity"})
    try:
        with urllib.request.build_opener(_PublicRedirects()).open(req, timeout=timeout) as response:
            validate_url(response.url)
            advertised = response.headers.get("Content-Length")
            if advertised and int(advertised) > max_bytes:
                raise AdapterError(f"Файл превышает допустимый размер {max_bytes} байт")
            chunks, length = [], 0
            while True:
                chunk = response.read(min(65536, max_bytes - length + 1))
                if not chunk:
                    break
                length += len(chunk)
                if length > max_bytes:
                    raise AdapterError(f"Файл превышает допустимый размер {max_bytes} байт")
                chunks.append(chunk)
            content = b"".join(chunks)
            return Download(content, response.url, response.headers.get("Content-Type", ""), hashlib.sha256(content).hexdigest(), response.headers.get("ETag", ""), response.headers.get("Last-Modified", ""))
    except urllib.error.HTTPError as exc:
        raise AdapterError(f"HTTP {exc.code} от источника {urllib.parse.urlsplit(url).hostname}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError(f"Ошибка загрузки {urllib.parse.urlsplit(url).hostname}: {exc}") from exc


def _attr(source, name, default=""):
    return source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)


def discover(source):
    """Find registered publication links; FNS index links are followed one level.

    Discovery fails explicitly on unreachable pages; no silent success using a
    previous file. A configured direct URL is checked as well as new page links.
    """
    adapter = _attr(source, "adapter")
    mapping = _attr(source, "mapping", {}) or {}
    page = _attr(source, "publications_url") or _attr(source, "url")
    direct = _attr(source, "url")
    if not page:
        raise AdapterError("Не задан адрес публикаций или файла")
    if mapping.get("direct_only") or re.search(r"\.(xlsx?|csv|json)(?:[?#]|$)", page, re.I):
        return [{"url": page, "title": Path(urllib.parse.urlsplit(page).path).name, "published_at": None}]
    response = fetch_url(page)
    soup = BeautifulSoup(response.content, "html.parser")
    pattern = mapping.get("link_pattern")
    if not pattern:
        pattern = {
            "cbr_deposits": r"02_27_dep_ind_excluding_escrow\.xlsx",
            "cbr_cards": r"/t6_\d{4}\.xlsx",
            "rosstat_income": r"urov[_-]10subg[_-]nm\.xlsx",
            "fns_social": r"/5ddk/\d+/",
        }.get(adapter, r"\.(xlsx|csv|json)(?:[?#]|$)")
    try:
        rx = re.compile(pattern, re.I)
    except re.error as exc:
        raise AdapterError("Некорректное выражение поиска ссылок") from exc
    candidates = []
    for a in soup.find_all("a", href=True):
        url = urllib.parse.urljoin(response.url, a["href"].replace("\\", "/"))
        title = a.get_text(" ", strip=True)
        if rx.search(urllib.parse.unquote(url)) or (adapter == "rosstat_income" and re.search(r"Среднедушевые денежные доходы населения по субъектам", title, re.I)):
            if adapter == "cbr_cards":
                section = a.find_parent(class_="document-regular")
                context = _norm(section.get_text(" ", strip=True)) if section else ""
                if not all(word in context for word in ("расчетных", "кредитных", "карт", "территориальном")):
                    continue
            if adapter == "fns_social" and not re.search(r"\.xlsx", url, re.I):
                release = fetch_url(url)
                release_soup = BeautifulSoup(release.content, "html.parser")
                text = release_soup.get_text(" ", strip=True)
                published = _publication_date(text)
                for link in release_soup.find_all("a", href=True):
                    file_url = urllib.parse.urljoin(release.url, link["href"])
                    if re.search(r"/77_5DDK_\d{8}\.xlsx", file_url, re.I):
                        candidates.append({"url": file_url, "title": title or text[:150], "published_at": published})
            else:
                candidates.append({"url": url, "title": title, "published_at": None})
    if direct and direct != page and not any(c["url"].casefold() == direct.casefold() for c in candidates):
        candidates.append({"url": direct, "title": "Зарегистрированный файл", "published_at": mapping.get("published_at")})
    unique = {c["url"]: c for c in candidates}
    if not unique:
        raise AdapterError("На зарегистрированной странице не найдены подходящие публикации; проверьте структуру и правило поиска")
    return sorted(unique.values(), key=lambda c: c["url"])


def _publication_date(text):
    match = re.search(r"Дата публикации[:\s]+(\d{2}\.\d{2}\.\d{4})", text)
    return datetime.strptime(match[1], "%d.%m.%Y").date().isoformat() if match else None


def _norm(value):
    return " ".join(str("" if value is None else value).lower().replace("ё", "е").replace("\xa0", " ").split())


def _territory(value):
    text = re.sub(r"^г[.\s]*", "", _norm(value))
    return text


def _decimal(value):
    if value is None or _norm(value) in {"", "…", "...", "-", "—", "нет данных", "н/д", "данные отсутствуют"}:
        return None, "missing"
    if _norm(value) in {"не применимо", "неприменимо", "n/a"}:
        return None, "na"
    try:
        number = Decimal(str(value).replace("\xa0", "").replace(" ", "").replace(",", ".").strip())
    except InvalidOperation as exc:
        raise AdapterError(f"Ожидалось числовое значение, получено: {str(value)[:100]}") from exc
    if not number.is_finite():
        raise AdapterError("Бесконечные и NaN-значения запрещены")
    return format(number, "f"), "value"


def _iso(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    match = re.search(r"\b(\d{2}\.\d{2}\.\d{4})\b", text)
    if match:
        return datetime.strptime(match[1], "%d.%m.%Y").date().isoformat()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise AdapterError(f"Не распознана дата: {text[:80]}") from exc


def _record(config, value, start, end=None, kind="flow", label="", provenance=None, **extra):
    number, state = _decimal(value)
    unit = config.get("unit")
    code = config.get("indicator_code")
    territory = config.get("territory")
    if not code or not unit or not territory:
        raise AdapterError("Задайте indicator_code, unit и territory в параметрах загрузчика")
    start = _iso(start)
    end = _iso(end or start)
    if start > end:
        raise AdapterError("Начало периода позже окончания")
    if kind not in {"stock", "flow", "cumulative"}:
        raise AdapterError("Укажите period_kind: stock, flow или cumulative")
    return {"indicator_code": code, "value": number, "data_state": state, "unit": unit, "territory": territory, "period_start": start, "period_end": end, "period_kind": kind, "period_label": label or (start if start == end else f"{start} — {end}"), "group": "", "answer": "", "numerator": None, "denominator": None, "provenance": provenance or {}, **extra}


def _workbook(path):
    try:
        with zipfile.ZipFile(path) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_UNCOMPRESSED:
                raise AdapterError("Распакованный Excel превышает допустимый размер")
        w = load_workbook(path, read_only=False, data_only=True, keep_links=False)
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise AdapterError("Ожидается файл XLSX (старый XLS сохраните как XLSX)") from exc
    if any(s.max_row > MAX_ROWS or s.max_column > 1000 for s in w):
        w.close()
        raise AdapterError("Таблица превышает допустимое число строк или столбцов")
    return w


def _cbr_deposits(path, config):
    w = _workbook(path)
    try:
        sheet_name = config.get("sheet", "итого")
        if sheet_name not in w.sheetnames:
            raise AdapterError("Не найден лист «итого»; структура файла Банка России изменилась")
        s = w[sheet_name]
        if "без учета счетов эскроу" not in _norm(s["A1"].value) or "млн руб" not in _norm(s["A1"].value):
            raise AdapterError("Заголовок/единицы файла вкладов не соответствуют определению без эскроу, млн руб.")
        if config.get("unit") != "млн руб.":
            raise AdapterError("Единица источника вкладов — млн руб.; автоматическое преобразование не настроено")
        rows = [r for r in range(3, s.max_row + 1) if _territory(s.cell(r, 1).value) == _territory(config.get("territory"))]
        if len(rows) != 1:
            raise AdapterError("Территория вкладов не найдена или неоднозначна")
        result = []
        for col in range(2, s.max_column + 1):
            if not s.cell(2, col).value:
                continue
            at = _iso(s.cell(2, col).value)
            cell = s.cell(rows[0], col)
            result.append(_record(config, cell.value, at, kind="stock", label=f"На {at}", provenance={"sheet": s.title, "cell": cell.coordinate, "header_cell": s.cell(2, col).coordinate, "territory_cell": f"A{rows[0]}"}))
        return result
    finally:
        w.close()


def _cbr_cards(path, config):
    if not config.get("definition_confirmed") or config.get("metric") not in {"total", "debit", "credit"}:
        raise AdapterError("Определение карт не согласовано: в реестре значения кредитных карт при названии «расчётные и кредитные». Подтвердите definition_confirmed и metric total/debit/credit")
    if config.get("unit") != "тыс. ед.":
        raise AdapterError("Единица территориальной таблицы 6 — тыс. ед.")
    col = {"total": 3, "debit": 4, "credit": 5}[config["metric"]]
    w = _workbook(path)
    try:
        result = []
        for s in w:
            if not re.fullmatch(r"на\s+\d{2}\.\d{2}\.\d{4}", s.title, re.I):
                continue
            if "карт" not in _norm(s["A1"].value) or "территориальн" not in _norm(s["A1"].value):
                raise AdapterError("Заголовок не соответствует территориальной таблице 6")
            if "тыс. ед" not in _norm(s["G3"].value):
                raise AdapterError("Изменились единицы таблицы карт")
            expected = {3: "всего", 4: "расчетные", 5: "кредитные"}[col]
            header = s.cell(5 if col == 3 else 6, col).value
            if expected not in _norm(header):
                raise AdapterError("Изменилась структура столбцов таблицы карт")
            rows = [r for r in range(8, s.max_row + 1) if _territory(s.cell(r, 2).value) == _territory(config.get("territory"))]
            if len(rows) != 1:
                raise AdapterError("Территория не найдена или неоднозначна в таблице карт")
            cell = s.cell(rows[0], col)
            at = _iso(s["A2"].value)
            result.append(_record(config, cell.value, at, kind="stock", label=f"На {at}", provenance={"sheet": s.title, "cell": cell.coordinate, "metric": config["metric"], "definition_confirmed": True}))
        if not result:
            raise AdapterError("Не найдены датированные листы территориальной таблицы 6")
        return result
    finally:
        w.close()


def _wide_series(path, config, kind):
    """Explicit headers/cells for offline Rosstat/FNS; never infer unit/period."""
    if not config.get("structure_confirmed"):
        raise AdapterError("Структура этого источника не проверена на доступном официальном файле. Загрузите сохранённый XLSX, задайте cells и structure_confirmed=true после сверки")
    if not config.get("cells"):
        raise AdapterError("Задайте cells: sheet, value_cell, period_start, period_end, period_kind и period_label")
    return _generic_xlsx(path, config)


def _generic_xlsx(path, config):
    w = _workbook(path)
    try:
        result = []
        for marker in config.get("expected_markers", []):
            marker_sheet = marker.get("sheet", config.get("sheet", w.sheetnames[0]))
            if marker_sheet not in w.sheetnames or _norm(marker["text"]) not in _norm(w[marker_sheet][marker["cell"]].value):
                raise AdapterError(f"Изменился контрольный заголовок/территория/единица: {marker_sheet}!{marker['cell']}")
        if config.get("cells"):
            for spec in config["cells"]:
                sheet = spec.get("sheet", config.get("sheet"))
                if sheet not in w.sheetnames:
                    raise AdapterError(f"Не найден лист {sheet}")
                s = w[sheet]
                cell = s[spec["value_cell"]]
                if spec.get("expected_header"):
                    actual = str(s[spec["header_cell"]].value or "")
                    if _norm(spec["expected_header"]) not in _norm(actual):
                        raise AdapterError(f"Изменился заголовок {sheet}!{spec['header_cell']}")
                merged = {**config, **spec}
                result.append(_record(merged, cell.value, spec["period_start"], spec.get("period_end"), spec.get("period_kind", "flow"), spec.get("period_label", ""), {"sheet": sheet, "cell": cell.coordinate, "configured_mapping": True}))
        else:
            sheet = config.get("sheet", w.sheetnames[0])
            if sheet not in w.sheetnames:
                raise AdapterError(f"Не найден лист {sheet}")
            s = w[sheet]
            header_row = int(config.get("header_row", 1))
            headers = [str(c.value or "").strip() for c in s[header_row]]
            if len(headers) != len(set(headers)) and any(headers):
                # Empty trailing headers are harmless; duplicate populated headers aren't.
                populated = [h for h in headers if h]
                if len(populated) != len(set(populated)):
                    raise AdapterError("Повторяющиеся имена столбцов")
            rows = []
            for cells in s.iter_rows(min_row=header_row + 1):
                if any(c.value is not None for c in cells):
                    rows.append(({h: c.value for h, c in zip(headers, cells) if h}, {"sheet": s.title, "row": cells[0].row}))
            result = _mapped_rows(rows, config)
        return _validate_records(result, config)
    finally:
        w.close()


def _mapped_rows(rows, config):
    mapping = config.get("fields", {})
    out = []
    for row, provenance in rows:
        record = {k: row.get(v) for k, v in mapping.items()} if mapping else dict(row)
        if config.get("territory_filter"):
            if _territory(record.get("territory")) != _territory(config["territory_filter"]):
                continue
        merged = {**config, **{k: v for k, v in record.items() if v is not None}}
        if not merged.get("period_start"):
            raise AdapterError("Не задан период; дата загрузки не заменяет дату наблюдения")
        value = record.get("value")
        r = _record(merged, value, merged["period_start"], merged.get("period_end"), merged.get("period_kind", "flow"), merged.get("period_label", ""), provenance)
        for key in ("group", "answer", "question_version", "base"):
            if record.get(key) is not None:
                r[key] = str(record[key])
        for key in ("numerator", "denominator"):
            if record.get(key) is not None:
                r[key], state = _decimal(record[key])
                if state != "value":
                    r[key] = None
        if record.get("data_state") in {"missing", "na"}:
            if r["value"] is not None:
                raise AdapterError("Статус отсутствующих данных несовместим с числом")
            r["data_state"] = record["data_state"]
        out.append(r)
    return out


def _validate_records(records, config):
    seen = set()
    for r in records:
        if config.get("territory") and _territory(r["territory"]) != _territory(config["territory"]):
            raise AdapterError("Территория записи не соответствует настройке источника")
        if config.get("unit") and r["unit"] != config["unit"]:
            raise AdapterError("Единица записи не соответствует настройке источника")
        key = tuple(str(r.get(k, "")) for k in ("indicator_code", "territory", "period_start", "period_end", "period_kind", "group", "answer", "question_version"))
        if key in seen:
            raise AdapterError("Дубли наблюдений внутри загружаемого файла")
        seen.add(key)
        if r.get("denominator") is not None and Decimal(r["denominator"]) <= 0:
            raise AdapterError("Знаменатель должен быть положительным")
        if r.get("numerator") is not None and r.get("denominator") is not None:
            if not 0 <= Decimal(r["numerator"]) <= Decimal(r["denominator"]):
                raise AdapterError("Числитель вне диапазона 0…знаменатель")
    return records


def parse_file(path, adapter, config):
    config = dict(config or {})
    path = Path(path)
    if path.stat().st_size > MAX_BYTES:
        raise AdapterError("Файл превышает 25 МиБ")
    if adapter == "cbr_deposits":
        records = _cbr_deposits(path, config)
    elif adapter == "cbr_cards":
        records = _cbr_cards(path, config)
    elif adapter in {"rosstat_income", "fns_social"}:
        records = _wide_series(path, config, adapter)
    elif adapter == "xlsx":
        records = _generic_xlsx(path, config)
    elif adapter == "json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            data = data.get(config.get("records_key", "records"))
        if not isinstance(data, list) or len(data) > MAX_ROWS or not all(isinstance(r, dict) for r in data):
            raise AdapterError("JSON должен содержать массив записей (не более 50000)")
        records = _mapped_rows([(r, {"row": i + 1}) for i, r in enumerate(data)], config)
    elif adapter == "csv":
        with path.open(encoding=config.get("encoding", "utf-8-sig"), newline="") as stream:
            reader = csv.DictReader(stream, delimiter=config.get("delimiter", ";"))
            rows = []
            for i, row in enumerate(reader, 2):
                if i > MAX_ROWS:
                    raise AdapterError("Слишком много строк CSV")
                rows.append((row, {"row": i}))
        records = _mapped_rows(rows, config)
    else:
        raise AdapterError(f"Неизвестный адаптер: {adapter}")
    if not records:
        raise AdapterError("Файл не содержит записей по заданным условиям")
    return _validate_records(records, config)


PILOT_SOURCES = [
    {"name": "Банк России · вклады без эскроу · Москва", "url": "https://www.cbr.ru/vfs/statistics/banksector/borrowings/02_27_dep_ind_excluding_escrow.xlsx", "publications_url": "https://www.cbr.ru/statistics/bank_sector/sors/", "adapter": "cbr_deposits", "territory": "Москва", "unit": "млн руб.", "mapping": {"indicator_code": "cbr_deposits_moscow", "territory": "Москва", "unit": "млн руб.", "sheet": "итого"}},
    {"name": "Банк России · карты, таблица 6 · требуется согласование", "url": "", "publications_url": "https://www.cbr.ru/statistics/nps/psrf/", "adapter": "cbr_cards", "territory": "Москва", "unit": "тыс. ед.", "mapping": {"indicator_code": "cbr_cards_moscow", "territory": "Москва", "unit": "тыс. ед.", "definition_confirmed": False, "metric": "", "hold_reason": "В реестре название всех карт, но числа соответствуют кредитным. Выберите total/debit/credit и подтвердите определение."}},
    {"name": "Росстат · среднедушевые денежные доходы · Москва", "url": "", "publications_url": "https://rosstat.gov.ru/folder/13397", "adapter": "rosstat_income", "territory": "Москва", "unit": "руб./мес.", "mapping": {"indicator_code": "rosstat_income_moscow", "territory": "Москва", "unit": "руб./мес.", "structure_confirmed": False, "cells": [], "hold_reason": "Сервер Росстата возвращает HTTP503. Требуется официальный XLSX для проверки структуры и раздельных квартальных/годовых периодов."}},
    {"name": "ФНС · социальные вычеты 5-ДДК · Москва", "url": "", "publications_url": "https://www.nalog.gov.ru/rn77/related_activities/statistics_and_analytics/forms/5ddk/", "adapter": "fns_social", "territory": "Москва", "unit": "чел.", "mapping": {"indicator_code": "fns_social_moscow", "territory": "Москва", "unit": "чел.", "structure_confirmed": False, "cells": [], "hold_reason": "Индекс выпусков доступен, файл Москвы HTTP403. Нужен сохранённый официальный XLSX и сверка строки числа налогоплательщиков, а не суммы вычетов."}},
]
