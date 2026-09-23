"""Автоподбор демонстрационных запросов под загруженные данные.

Нужен, чтобы показать обязательные сценарии из Definition of Done на любом
датасете: демо-наборе или настоящем. Результат детерминирован.
"""
import datetime as dt
import statistics

from .data import norm
from .engine import FORMATS, recommend

DENSE = ["Ведущий", "Фотограф", "Банкетный зал"]
RARE = ["Флорист", "Декоратор", "Подарки и сувениры", "Ведущий церемонии",
        "Фото и видеобудки", "Отель", "Инструменталист"]
AUTUMN = [dt.date(2026, 9, 26) + dt.timedelta(days=i) for i in range(66)]  # 26.09 – 30.11
WINTER = [dt.date(2026, 12, 1) + dt.timedelta(days=i) for i in range(31)]


def pool(vendors, cat, city=None):
    return [v for v in vendors if norm(cat) in v.cats_n and (city is None or norm(v.city) == norm(city))]


def top_city(vendors, cat):
    cities = {}
    for v in pool(vendors, cat):
        cities[v.city] = cities.get(v.city, 0) + 1
    return max(sorted(cities), key=lambda c: cities[c]) if cities else None


def top_format(vs):
    counts = {f: sum(f in v.formats_n for v in vs) for f in FORMATS}
    return max(FORMATS, key=lambda f: counts[f])


def _ids(res):
    return [c["id"] for c in res["cards"]]


def dense_pair(vendors):
    """Плотная категория: две осенние даты с тремя карточками и разной выдачей из-за занятости."""
    cat = max(DENSE, key=lambda c: (len(pool(vendors, c, top_city(vendors, c))), -DENSE.index(c)))
    city = top_city(vendors, cat)
    if not city:
        return []
    vs = pool(vendors, cat, city)
    fmt = top_format(vs)
    prices = [v.price for v in vs if v.price]
    med = statistics.median(prices) if prices else 500_000
    fallback = None
    for mult in (1.3, 1.6, 2.0, 3.0):
        budget = int(round(med * mult, -4))
        base = {"city": city, "event_format": fmt, "category": cat, "budget": budget}
        first = None
        for d in AUTUMN:
            res = recommend(vendors, {**base, "date": d.isoformat()})
            if not (res["outcome"] == "found" and len(res["cards"]) == 3 and res["rejected"]):
                continue
            if first is None:
                first = (d, res)
                fallback = fallback or [{**base, "date": d.isoformat()}]
            elif _ids(res) != _ids(first[1]) and (res.get("date_effect") or first[1].get("date_effect")):
                a, b = (first, (d, res)) if first[1].get("date_effect") else ((d, res), first)
                return [{**base, "date": a[0].isoformat()}, {**base, "date": b[0].isoformat()}]
    return fallback or [{"city": city, "event_format": fmt, "category": cat,
                         "budget": int(med * 2), "date": AUTUMN[0].isoformat()}]


def rare(vendors):
    for rc in RARE:
        city = top_city(vendors, rc)
        if not city:
            continue
        vs = pool(vendors, rc, city)
        fmt = top_format(vs)
        budget = max(v.price or 0 for v in vs) or 1_000_000
        for d in AUTUMN[::3]:
            p = {"city": city, "date": d.isoformat(), "event_format": fmt, "category": rc, "budget": budget}
            if recommend(vendors, p)["outcome"] == "found":
                return p
    return None


def no_category(vendors):
    cities = sorted({v.city for v in vendors if v.city})
    for rc in RARE + DENSE:
        missing = [c for c in cities if not pool(vendors, rc, c)]
        if missing:
            return {"city": missing[0], "date": "2026-11-14", "event_format": "свадьба",
                    "category": rc, "budget": 500_000}
    return None


def no_match(vendors):
    cities = sorted({v.city for v in vendors if v.city})
    for rc in RARE + DENSE:
        for c in cities:
            vs = pool(vendors, rc, c)
            if len(vs) < 2:
                continue
            fmt = top_format(vs)
            for d in WINTER:
                if d.weekday() < 5:
                    continue
                p = {"city": c, "date": d.isoformat(), "event_format": fmt, "category": rc,
                     "budget": max(v.price or 0 for v in vs)}
                if recommend(vendors, p)["outcome"] == "no_match":
                    return p
    return None


def find_all(vendors) -> list[dict]:
    out = []
    pair = dense_pair(vendors)
    if pair:
        out.append({"key": "dense", "title": f"Плотная категория: {pair[0]['category']}, {pair[0]['date']}",
                    "params": pair[0]})
    if len(pair) > 1:
        out.append({"key": "dense_other_date",
                    "title": f"Тот же запрос на {pair[1]['date']}: выдача меняется из-за занятости",
                    "params": pair[1]})
    for key, fn, title in (("rare", rare, "Редкая категория"),
                           ("no_category", no_category, "В этом городе такой категории нет"),
                           ("no_match", no_match, "Кандидаты есть, но никто не подходит")):
        p = fn(vendors)
        if p:
            out.append({"key": key, "title": f"{title}: {p['category']}, {p['city']}", "params": p})
    return out
