"""Пайплайн подбора: валидация -> кандидаты -> жёсткие фильтры -> скоринг -> объяснения.

Всё детерминировано: нет случайности, нет сетевых вызовов, сортировка
с явным тай-брейком по цене и id. Одинаковый запрос даёт одинаковый ответ.
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
import time
from dataclasses import dataclass

from . import text as T
from .data import WINDOW_END, WINDOW_START, Vendor, norm, parse_date

FORMATS = ["свадьба", "той", "корпоратив", "конференция", "юбилей", "день рождения"]
LANGUAGES = ["русский", "казахский", "английский"]

# Основы слов для поиска смыслового совпадения формата в свободном описании.
FORMAT_STEMS = {
    "свадьба": ["свадеб", "свадьб", "невест", "молодож", "жених", "wedding"],
    "той": ["той", "тои", "тоя", "тоев", "тоях", "узату", "беташар", "сундет", "тусау", "шашу", "традиц"],
    "корпоратив": ["корпоратив", "компани", "тимбилдинг", "коллектив", "сотрудник", "новогодн"],
    "конференция": ["конференц", "форум", "делов", "презентац", "выставк", "спикер", "панельн", "бизнес"],
    "юбилей": ["юбиле"],
    "день рождения": ["день рождения", "дни рождения", "дня рождения", "детск", "аниматор"],
}
WORD_STEM_EXACT = {"той", "тои", "тоя", "тоев", "тоях"}  # короткие основы: только целым словом

# Жёсткие условия в порядке проверки. Первая неудачная считается основной причиной отказа.
CHECK_ORDER = ["busy", "format", "budget", "language", "hours"]

WEIGHTS = {"relevance": 0.35, "budget": 0.30, "language": 0.15, "hours": 0.10, "quality": 0.10}


class RequestError(ValueError):
    pass


@dataclass(frozen=True)
class Request:
    city: str
    date: dt.date
    event_format: str
    category: str
    budget: int
    hours: float | None = None
    language: str | None = None

    @classmethod
    def from_params(cls, p: dict) -> "Request":
        errors = []
        city = str(p.get("city") or "").strip()
        category = str(p.get("category") or "").strip()
        if not city:
            errors.append("не указан город")
        if not category:
            errors.append("не указана категория подрядчика")
        date = parse_date(p.get("date"))
        if not date:
            errors.append("дата должна быть в формате ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")
        fmt = norm(p.get("event_format") or p.get("format"))
        if fmt not in FORMATS:
            errors.append(f"тип мероприятия должен быть одним из: {', '.join(FORMATS)}")
        try:
            budget = int(float(str(p.get("budget", "")).replace(" ", "").replace(" ", "")))
            if budget <= 0:
                raise ValueError
        except ValueError:
            budget = 0
            errors.append("бюджет должен быть положительным числом в тенге")
        hours = None
        if p.get("hours") not in (None, ""):
            try:
                hours = float(str(p["hours"]).replace(",", "."))
                if hours <= 0:
                    raise ValueError
            except ValueError:
                hours = None
                errors.append("длительность должна быть положительным числом часов")
        lang = norm(p.get("language")) or None
        if lang and lang not in LANGUAGES:
            errors.append(f"язык должен быть одним из: {', '.join(LANGUAGES)}")
        if errors:
            raise RequestError("; ".join(errors))
        return cls(city, date, fmt, category, budget, hours, lang)


# ---------- признаки из профиля ----------

def _stem_hit(text: str, stem: str) -> bool:
    if stem in WORD_STEM_EXACT:
        return re.search(rf"(?<![а-яa-z]){stem}(?![а-яa-z])", text) is not None
    return stem in text


def sentences(desc: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n+", desc) if s.strip()]


def snippet(s: str, limit: int = 110) -> str:
    s = s.strip().rstrip(".;")
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",;:—- ") + "…"


def format_quote(v: Vendor, fmt: str) -> str | None:
    """Первое предложение описания, где по смыслу говорится о запрошенном формате."""
    for s in sentences(v.description):
        low = norm(s)
        if any(_stem_hit(low, st) for st in FORMAT_STEMS[fmt]):
            return snippet(s)
    return None


def experience_years(desc: str) -> int | None:
    low = norm(desc)
    m = re.search(r"опыт\w*[^.]{0,25}?(\d{1,2})\s*\+?\s*(?:лет|года|год)", low) or \
        re.search(r"(\d{1,2})\s*\+?\s*(?:лет|года|год)\s+(?:опыт|в профессии|работ|на рынке)", low)
    if m:
        return int(m.group(1))
    m = re.search(r"\bс\s+(19[89]\d|20[0-2]\d)\s+год", low)
    if m:
        return WINDOW_START.year - int(m.group(1))
    return None


def check(v: Vendor, r: Request) -> list[str]:
    """Список нарушенных жёстких условий. Пустой список значит, что подрядчик подходит."""
    failed = []
    if v.is_busy(r.date):
        failed.append("busy")
    if v.formats_n and r.event_format not in v.formats_n:
        failed.append("format")
    if v.price is not None and v.price > r.budget:
        failed.append("budget")
    if r.language and v.langs_n and r.language not in v.langs_n:
        failed.append("language")
    if r.hours and v.max_hours is not None and v.max_hours < r.hours:
        failed.append("hours")
    return failed


def score(v: Vendor, r: Request) -> dict:
    parts = {}
    # смысл описания + узость специализации
    hit = 1.0 if format_quote(v, r.event_format) else 0.0
    spec = 1.0 if 0 < len(v.formats_n) <= 2 else 0.5 if len(v.formats_n) == 3 else 0.0
    parts["relevance"] = 0.7 * hit + 0.3 * spec
    # бюджет: до 70% бюджета максимум, дальше линейно до 0.5 на границе
    if v.price is None:
        parts["budget"] = 0.4
    else:
        ratio = v.price / r.budget
        parts["budget"] = 1.0 if ratio <= 0.7 else max(0.0, 1.0 - (ratio - 0.7) / 0.3 * 0.5)
    # язык
    if r.language:
        if not v.langs_n:
            parts["language"] = 0.3
        else:
            parts["language"] = 1.0 if T.LANG_STEM[r.language] in norm(v.description) else 0.8
    else:
        kaz_needed = r.event_format in ("той", "свадьба")
        parts["language"] = 1.0 if (kaz_needed and "казахский" in v.langs_n) else \
            min(1.0, 0.5 + 0.2 * max(0, len(v.langs_n) - 1))
    # длительность
    if v.max_hours is None:
        parts["hours"] = 0.8 if r.hours else 0.6
    elif r.hours:
        parts["hours"] = min(1.0, 0.6 + 0.1 * (v.max_hours - r.hours))
    else:
        parts["hours"] = min(1.0, v.max_hours / 10)
    # качество данных: настоящий профиль надёжнее синтетического или достроенного
    parts["quality"] = 0.7 if v.synthetic else 0.85 if (v.city_imputed or v.price_imputed) else 1.0
    total = sum(WEIGHTS[k] * parts[k] for k in WEIGHTS)
    return {"total": round(total, 6), "parts": {k: round(x, 3) for k, x in parts.items()}}


def rank_key(item):
    v, sc = item
    return (-sc["total"], v.price if v.price is not None else 10**12, v.id)


def nearest_free(v: Vendor, d: dt.date, span: int = 14, limit: int = 2) -> list[dt.date]:
    out = []
    for k in range(1, span + 1):
        for cand in (d - dt.timedelta(days=k), d + dt.timedelta(days=k)):
            if WINDOW_START <= cand <= WINDOW_END and not v.is_busy(cand):
                out.append(cand)
        if len(out) >= limit:
            break
    return sorted(out[:limit])


# ---------- объяснения ----------

def explain(v: Vendor, r: Request, shown: list[Vendor]) -> str:
    """1–2 предложения только из фактов этого профиля и его отличий от соседей по выдаче."""
    others = [o for o in shown if o.id != v.id]
    facts = []  # (приоритет, текст)

    quote = format_quote(v, r.event_format)
    if quote:
        same = any(format_quote(o, r.event_format) == quote for o in others)
        facts.append((52 if same else 90, f"в описании прямо про {T.FORMAT_PLURAL[r.event_format]}: «{quote}»"))
    elif v.formats:
        facts.append((52, f"формат «{r.event_format}» заявлен в профиле, но в описании о нём прямо не сказано"))

    if 0 < len(v.formats) <= 2:
        facts.append((70, f"берёт только {T.join_ru(['«' + f + '»' for f in v.formats])}, узкая специализация"))

    if r.language and r.language in v.langs_n:
        unique = bool(others) and all(r.language not in o.langs_n for o in others)
        in_desc = T.LANG_STEM[r.language] in norm(v.description)
        t = f"работает {T.LANG_ON[r.language]}"
        if in_desc:
            t += ", это подтверждено и в описании"
        if unique:
            t += ", единственный такой среди подобранных"
        facts.append((85 if unique else 60 if in_desc else 40, t))
    elif not r.language and "казахский" in v.langs_n and r.event_format in ("той", "свадьба"):
        if not others or any("казахский" not in o.langs_n for o in others):
            facts.append((62, "работает и на казахском, что пригодится для казахской части программы"))

    if r.hours:
        if v.max_hours is not None:
            head = v.max_hours - r.hours
            t = f"на площадке до {T.hours(v.max_hours)} при ваших {T.hours(r.hours)}"
            t += f", запас {T.hours(head)}" if head > 0 else ", ровно впритык"
            longest = bool(others) and all(o.max_hours is None or o.max_hours < v.max_hours for o in others)
            facts.append((66 if longest else 48, t))
        else:
            facts.append((45, f"работа не привязана к часам на площадке, поэтому {T.hours(r.hours)} не ограничение"))
    elif v.max_hours is not None and others and all(
            o.max_hours is not None and o.max_hours < v.max_hours for o in others):
        facts.append((55, f"самый длинный рабочий день среди подобранных, до {T.hours(v.max_hours)}"))

    yrs = experience_years(v.description)
    if yrs:
        most = bool(others) and all((experience_years(o.description) or 0) < yrs for o in others)
        facts.append((58 if most else 50, f"{yrs} {T.plural(yrs, 'год', 'года', 'лет')} опыта по описанию"
                      + (", больше всех в подборке" if most else "")))

    if not v.formats:
        facts.append((10, "принимаемые форматы в профиле не указаны, формат стоит уточнить"))

    facts.sort(key=lambda x: -x[0])

    # Бюджетный факт есть всегда: он с конкретными числами и различает карточки.
    if v.price is None:
        budget = "цена в профиле не указана, её нужно уточнить"
    else:
        pct = round(v.price / r.budget * 100)
        budget = f"цена от {T.money(v.price)}, это {pct}% бюджета"
        prices = [o.price for o in others if o.price is not None]
        if prices and v.price < min(prices):
            budget += ", самый доступный из подобранных"
        elif prices and v.price > max(prices):
            budget += ", самый дорогой из подобранных"
        elif pct >= 95:
            budget += ", почти впритык к бюджету"
        elif pct <= 60:
            budget += f", остаётся {T.money(r.budget - v.price)}"
        if v.price_imputed:
            budget += " (цена проставлена при подготовке данных)"

    s1 = T.cap(facts[0][1]) + "."
    s2 = T.cap(budget)
    if len(facts) > 1 and facts[1][0] >= 50:
        s2 += "; " + facts[1][1]
    return s1 + " " + s2 + "."


# ---------- основной вызов ----------

def _reason_text(key: str, r: Request, c: int) -> str:
    if key == "busy":
        return f"{c} {T.plural(c, 'занят', 'заняты', 'заняты')} {T.ddate(r.date)}"
    if key == "format":
        return f"{c} не {T.plural(c, 'берёт', 'берут', 'берут')} формат «{r.event_format}»"
    if key == "budget":
        return f"{c} дороже бюджета {T.money(r.budget)}"
    if key == "language":
        return f"{c} не {T.plural(c, 'работает', 'работают', 'работают')} {T.LANG_ON[r.language]}"
    return f"{c} не {T.plural(c, 'работает', 'работают', 'работают')} {T.hours(r.hours)} подряд"


def vendor_reasons(v: Vendor, r: Request, failed: list[str]) -> list[str]:
    out = []
    for f in failed:
        if f == "busy":
            free = nearest_free(v, r.date)
            t = f"занят {T.ddate(r.date)}"
            if free:
                t += f", ближайшие свободные даты: {', '.join(T.ddate(d, True) for d in free)}"
            out.append(t)
        elif f == "format":
            out.append(f"не берёт «{r.event_format}» (берёт: {', '.join(v.formats)})")
        elif f == "budget":
            out.append(f"цена от {T.money(v.price)}, на {T.money(v.price - r.budget)} выше бюджета")
        elif f == "language":
            out.append(f"не работает {T.LANG_ON[r.language]} (языки: {', '.join(v.languages)})")
        elif f == "hours":
            out.append(f"максимум {T.hours(v.max_hours)} на площадке, нужно {T.hours(r.hours)}")
    return out


def _suggest_dates(pool: list[Vendor], r: Request, limit: int = 3) -> list[str]:
    found = []
    for k in range(1, 15):
        for d in (r.date - dt.timedelta(days=k), r.date + dt.timedelta(days=k)):
            if not (WINDOW_START <= d <= WINDOW_END):
                continue
            rd = Request(r.city, d, r.event_format, r.category, r.budget, r.hours, r.language)
            ok = sum(1 for v in pool if not check(v, rd))
            if ok:
                found.append((k, d, ok))
        if len(found) >= limit:
            break
    found = sorted(found[:limit], key=lambda x: x[1])
    return [f"{T.ddate(d, True)}: {T.plural(c, 'подходит', 'подходят', 'подходят')} {c}" for _, d, c in found]


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def recommend(vendors: list[Vendor], params: dict, top_n: int = 3) -> dict:
    t0 = time.perf_counter()
    try:
        r = Request.from_params(params)
    except RequestError as e:
        return {"outcome": "invalid_request", "outcome_label": "Запрос заполнен не полностью",
                "headline": f"Не можем подобрать: {e}.", "cards": [], "notes": [], "suggestions": [],
                "rejected": [], "elapsed_ms": _ms(t0)}

    cat_n, city_n = norm(r.category), norm(r.city)
    notes = []
    if not (WINDOW_START <= r.date <= WINDOW_END):
        notes.append(f"Дата {T.ddate_full(r.date)} вне окна календарей "
                     f"{WINDOW_START:%d.%m.%Y}–{WINDOW_END:%d.%m.%Y}: занятость на неё неизвестна, "
                     "поэтому все считаются свободными.")
    base = {"request": {"city": r.city, "date": r.date.isoformat(), "event_format": r.event_format,
                        "category": r.category, "budget": r.budget, "hours": r.hours, "language": r.language}}

    in_category = [v for v in vendors if cat_n in v.cats_n]
    pool = [v for v in in_category if norm(v.city) == city_n]

    # Исход 2: в городе нет такой категории
    if not pool:
        if in_category:
            by_city = {}
            for v in in_category:
                by_city[v.city] = by_city.get(v.city, 0) + 1
            where = ", ".join(f"{c}: {n}" for c, n in sorted(by_city.items(), key=lambda x: (-x[1], x[0])))
            headline = (f"{T.cap(T.city_in(r.city))} в каталоге нет подрядчиков категории «{r.category}». "
                        f"Эта категория есть в других городах ({where}).")
        else:
            all_cats = sorted({c for v in vendors for c in v.categories})
            close = difflib.get_close_matches(r.category, all_cats, n=3, cutoff=0.5)
            headline = f"Категории «{r.category}» нет в каталоге ни в одном городе."
            if close:
                headline += f" Возможно, вы имели в виду: {', '.join('«' + c + '»' for c in close)}."
        return {**base, "outcome": "no_category_in_city",
                "outcome_label": "В этом городе такой категории нет", "headline": headline,
                "cards": [], "notes": notes, "suggestions": [], "rejected": [], "date_effect": None,
                "stats": {"in_city": 0}, "elapsed_ms": _ms(t0)}

    passed, rejected = [], []
    for v in pool:
        failed = check(v, r)
        (rejected if failed else passed).append((v, failed))

    scored = sorted(((v, score(v, r)) for v, _ in passed), key=rank_key)
    top = scored[:top_n]
    shown = [v for v, _ in top]

    n = len(pool)
    who = f"{n} {T.plural(n, 'подрядчик', 'подрядчика', 'подрядчиков')}"
    of_who = f"{n} {T.plural(n, 'подрядчика', 'подрядчиков', 'подрядчиков')}"
    busy_total = sum(1 for v in pool if v.is_busy(r.date))
    counts = {}
    for _, failed in rejected:
        counts[failed[0]] = counts.get(failed[0], 0) + 1
    reason_line = T.join_ru([_reason_text(k, r, counts[k]) for k in CHECK_ORDER if k in counts])

    # Эффект даты: кто вошёл бы в тройку, если бы не был занят именно в этот день.
    busy_only = [(v, score(v, r)) for v, f in rejected if f == ["busy"]]
    date_effect = None
    if busy_only:
        merged = sorted(scored + busy_only, key=rank_key)[:top_n]
        blockers = [v for v, _ in merged if v.is_busy(r.date)]
        if blockers:
            k = len(blockers)
            names = T.join_ru([T.quoted(v.name) for v in blockers])
            if k == 1:
                date_effect = (f"Выдача зависит от даты: без учёта занятости в подборку вошёл бы профиль {names}. "
                               f"Он подходит по всем условиям, но на {T.ddate_full(r.date)} занят.")
            else:
                date_effect = (f"Выдача зависит от даты: без учёта занятости в подборку вошли бы профили {names}. "
                               f"Они подходят по всем условиям, но на {T.ddate_full(r.date)} заняты.")

    cards = []
    for i, (v, sc) in enumerate(top, 1):
        matched_cat = next(c for c in v.categories if norm(c) == cat_n)
        flags = []
        if v.city_imputed:
            flags.append("город проставлен при подготовке данных")
        if v.price_imputed:
            flags.append("цена проставлена при подготовке данных")
        cards.append({
            "rank": i, "id": v.id, "name": v.name, "category": matched_cat, "categories": v.categories,
            "city": v.city, "price_from_kzt": v.price,
            "price_text": f"от {T.money(v.price)}" if v.price is not None else "цена не указана",
            "explanation": explain(v, r, shown),
            "provenance": v.provenance, "synthetic": v.synthetic, "origin": v.origin, "flags": flags,
            "formats": v.formats, "languages": v.languages, "max_hours": v.max_hours,
            "score": sc["total"], "score_parts": sc["parts"],
        })

    rejected_out = [{"id": v.id, "name": v.name, "price_from_kzt": v.price, "provenance": v.provenance,
                     "reasons": vendor_reasons(v, r, f)} for v, f in sorted(rejected, key=lambda x: x[0].id)]
    stats = {"in_city": n, "passed": len(passed), "busy_on_date": busy_total, "rejected_by": counts}
    suggestions = []

    if not passed:
        # Исход 3: кандидаты есть, но никто не проходит
        headline = (f"{T.cap(T.city_in(r.city))} есть {who} категории «{r.category}», "
                    f"но ни один не подходит: {reason_line}.")
        dates = _suggest_dates(pool, r)
        if dates:
            suggestions.append("Соседние даты, где кто-то подходит по всем условиям: " + "; ".join(dates) + ".")
        else:
            suggestions.append("В пределах двух недель от этой даты подходящих по всем условиям нет.")
        only_budget = [v for v, f in rejected if f == ["budget"]]
        if only_budget:
            cheapest = min(only_budget, key=lambda v: (v.price, v.id))
            suggestions.append(f"При бюджете от {T.money(cheapest.price)} подошёл бы профиль {T.quoted(cheapest.name)}: "
                               f"в эту дату свободен и берёт формат «{r.event_format}».")
        return {**base, "outcome": "no_match",
                "outcome_label": "Кандидаты есть, но ни один не проходит по условиям",
                "headline": headline, "why_fewer": None, "cards": [], "notes": notes,
                "date_effect": date_effect, "suggestions": suggestions, "rejected": rejected_out,
                "stats": stats, "elapsed_ms": _ms(t0)}

    # Исход 1: подобрали
    k = len(cards)
    headline = (f"Подобрали {k} из {of_who} категории «{r.category}» {T.city_in(r.city)}. "
                f"На {T.ddate_full(r.date)} {T.plural(busy_total, 'занят', 'заняты', 'заняты')} "
                f"{busy_total} из {n}.")
    why_fewer = None
    if k < top_n:
        if not rejected:
            why_fewer = f"Показываем {k}, а не {top_n}: столько всего подрядчиков этой категории {T.city_in(r.city)}."
        else:
            why_fewer = f"Показываем {k}, а не {top_n}, потому что остальные не прошли условия: {reason_line}."
        dates = _suggest_dates(pool, r)
        if dates:
            suggestions.append("Если дата гибкая, вот соседние дни и сколько там подходящих: "
                               + "; ".join(dates) + ".")
    elif rejected:
        notes.append(f"Не прошли условия: {reason_line}.")
    return {**base, "outcome": "found", "outcome_label": "Подобрали", "headline": headline,
            "why_fewer": why_fewer, "date_effect": date_effect, "cards": cards, "notes": notes,
            "suggestions": suggestions, "rejected": rejected_out, "stats": stats, "elapsed_ms": _ms(t0)}
