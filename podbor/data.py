"""Загрузка и нормализация профилей подрядчиков.

Источники (в порядке приоритета):
  1. путь из переменной окружения PODBOR_DATA;
  2. data/hackathon-dataset-anonymized.(jsonl|csv|xlsx) — датасет хакатона;
  3. любой другой .jsonl/.csv/.xlsx в data/ (первый по алфавиту), например ваша Excel-база;
  4. data/demo-dataset.jsonl — сгенерированный демо-набор (все synthetic).
Поддерживаются форматы JSONL, CSV (в том числе «;» и Windows-1251 из русского Excel) и XLSX.
Дополнительно подмешивается data/extra-synthetic.jsonl, если он есть:
это профили, дописанные командой; они получают origin="extra".
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REAL_FILE = DATA_DIR / "hackathon-dataset-anonymized.jsonl"
DEMO_FILE = DATA_DIR / "demo-dataset.jsonl"
EXTRA_FILE = DATA_DIR / "extra-synthetic.jsonl"

WINDOW_START = dt.date(2026, 9, 23)
WINDOW_END = dt.date(2026, 12, 31)


def norm(s) -> str:
    return str(s or "").strip().lower().replace("ё", "е")


def as_list(v) -> list[str]:
    """Поле-список может прийти списком или строкой 'a; b' / 'a, b' / '["a","b"]'."""
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            return as_list(json.loads(s))
        except json.JSONDecodeError:
            s = s.strip("[]")
    return [p.strip().strip("'\"") for p in re.split(r"[;,|\n]", s) if p.strip().strip("'\"")]


def parse_date(s) -> dt.date | None:
    s = str(s or "").strip()
    # Excel хранит даты числом дней от 30.12.1899 (например, 46300 — это ноябрь 2026)
    if re.fullmatch(r"\d{5}(\.0+)?", s) and 30000 <= float(s) <= 80000:
        return dt.date(1899, 12, 30) + dt.timedelta(days=int(float(s)))
    s = s.split(" ")[0].split("T")[0]  # «2026-11-14 00:00:00» -> «2026-11-14»
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def as_bool(v) -> bool:
    return v is True or norm(v) in ("true", "1", "yes", "да", "истина", "+")


def as_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).replace(" ", "").replace(" ", "")))
    except ValueError:
        return None


@dataclass
class Vendor:
    id: str
    name: str
    categories: list[str]
    city: str
    price: int | None
    formats: list[str]
    languages: list[str]
    max_hours: float | None
    busy: frozenset
    description: str
    synthetic: bool
    city_imputed: bool
    price_imputed: bool
    origin: str  # dataset | demo | extra
    cats_n: frozenset = field(default=frozenset())
    formats_n: frozenset = field(default=frozenset())
    langs_n: frozenset = field(default=frozenset())

    def is_busy(self, d: dt.date) -> bool:
        return d in self.busy

    @property
    def provenance(self) -> str:
        if self.origin == "extra":
            return "добавлен командой (синтетический)"
        if self.synthetic:
            return "синтетический профиль"
        return "реальный профиль"


def _vendor(row: dict, origin: str) -> Vendor:
    cats = as_list(row.get("categories"))
    formats = as_list(row.get("event_formats"))
    langs = as_list(row.get("languages"))
    mh = row.get("max_hours")
    try:
        max_hours = float(mh) if mh not in (None, "", "null") else None
    except ValueError:
        max_hours = None
    busy = frozenset(d for d in (parse_date(x) for x in as_list(row.get("busy_dates"))) if d)
    return Vendor(
        id=str(row.get("id")),
        name=str(row.get("anon_name") or row.get("name") or row.get("id")),
        categories=cats,
        city=str(row.get("city") or "").strip(),
        price=as_int(row.get("price_from_kzt")),
        formats=formats,
        languages=langs,
        max_hours=max_hours,
        busy=busy,
        description=str(row.get("description") or "").strip(),
        synthetic=as_bool(row.get("synthetic")) or origin == "extra",
        city_imputed=as_bool(row.get("city_imputed")),
        price_imputed=as_bool(row.get("price_imputed")),
        origin=origin,
        cats_n=frozenset(norm(c) for c in cats),
        formats_n=frozenset(norm(f) for f in formats),
        langs_n=frozenset(norm(l) for l in langs),
    )


# Заголовки столбцов, которые встречаются в самодельных таблицах, -> поля схемы датасета.
HEADER_ALIASES = {
    "id": "id", "ид": "id", "№": "id", "номер": "id",
    "anon_name": "anon_name", "name": "anon_name", "имя": "anon_name", "название": "anon_name",
    "подрядчик": "anon_name", "наименование": "anon_name",
    "categories": "categories", "category": "categories", "категории": "categories", "категория": "categories",
    "city": "city", "город": "city",
    "price_from_kzt": "price_from_kzt", "price": "price_from_kzt", "цена": "price_from_kzt",
    "цена от": "price_from_kzt", "цена_от": "price_from_kzt", "цена от, тг": "price_from_kzt",
    "цена от, ₸": "price_from_kzt", "стоимость": "price_from_kzt",
    "event_formats": "event_formats", "formats": "event_formats", "форматы": "event_formats",
    "формат": "event_formats", "типы мероприятий": "event_formats", "тип мероприятия": "event_formats",
    "languages": "languages", "языки": "languages", "язык": "languages",
    "max_hours": "max_hours", "макс часов": "max_hours", "максимум часов": "max_hours", "часы": "max_hours",
    "busy_dates": "busy_dates", "занятые даты": "busy_dates", "занятость": "busy_dates", "занят": "busy_dates",
    "description": "description", "описание": "description",
    "synthetic": "synthetic", "синтетический": "synthetic",
    "city_imputed": "city_imputed", "price_imputed": "price_imputed",
}
REQUIRED = ["categories", "city", "busy_dates"]


def _canon_rows(rows: list[dict], path: Path) -> list[dict]:
    """Приводит заголовки к полям схемы и проверяет, что обязательные столбцы есть."""
    if not rows:
        return rows
    mapping = {}
    for h in rows[0].keys():
        key = norm(h).replace("_", " ") if norm(h) not in HEADER_ALIASES else norm(h)
        mapping[h] = HEADER_ALIASES.get(norm(h)) or HEADER_ALIASES.get(key) or norm(h)
    missing = [c for c in REQUIRED if c not in mapping.values()]
    if missing:
        raise ValueError(
            f"В файле {path.name} не найдены столбцы: {', '.join(missing)}. "
            f"Найдены заголовки: {', '.join(map(str, rows[0].keys()))}. "
            "Переименуйте столбцы как в README (раздел про данные).")
    out = []
    for i, r in enumerate(rows, 1):
        c = {mapping[k]: v for k, v in r.items() if k in mapping}
        if not str(c.get("id") or "").strip():
            c["id"] = f"row-{i:03d}"
        out.append(c)
    return out


def _read_csv(path: Path) -> list[dict]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    first = text.splitlines()[0] if text else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    # Текст целиком, а не по строкам: в ячейке может быть несколько дат через перенос строки.
    return list(csv.DictReader(io.StringIO(text, newline=""), delimiter=delim))


def _read_jsonl(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return _canon_rows(_read_csv(path), path)
    if suffix in (".xlsx", ".xlsm"):
        from .xlsx import read_xlsx
        return _canon_rows(read_xlsx(path), path)
    if suffix == ".xls":
        raise ValueError("Старый формат .xls не поддерживается. Откройте файл в Excel и сохраните как .xlsx.")
    rows = []
    with path.open(encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def resolve_main_file() -> tuple[Path, str]:
    env = os.environ.get("PODBOR_DATA")
    if env:
        return Path(env), "dataset"
    for suffix in (".jsonl", ".csv", ".xlsx"):
        if REAL_FILE.with_suffix(suffix).exists():
            return REAL_FILE.with_suffix(suffix), "dataset"
    skip = {DEMO_FILE.name, EXTRA_FILE.name}
    own = sorted(p for p in DATA_DIR.glob("*")
                 if p.suffix.lower() in (".jsonl", ".csv", ".xlsx", ".xlsm")
                 and p.name not in skip and not p.name.startswith("~$"))  # ~$ — служебный файл открытого Excel
    if own:
        return own[0], "dataset"
    return DEMO_FILE, "demo"


def load_vendors(path: Path | None = None) -> tuple[list[Vendor], dict]:
    main, origin = (path, "dataset") if path else resolve_main_file()
    if not main.exists():
        raise FileNotFoundError(
            f"Нет файла данных {main}. Положите датасет (.jsonl, .csv или .xlsx) в data/ "
            "или выполните: python scripts/make_demo_data.py")
    vendors = [_vendor(r, origin) for r in _read_jsonl(main)]
    extra = 0
    if path is None and EXTRA_FILE.exists():
        ex = [_vendor(r, "extra") for r in _read_jsonl(EXTRA_FILE)]
        extra = len(ex)
        vendors += ex
    vendors.sort(key=lambda v: v.id)  # стабильный порядок для детерминизма
    info = {"file": main.name, "origin": origin, "total": len(vendors), "extra": extra,
            "synthetic": sum(v.synthetic for v in vendors)}
    return vendors, info
