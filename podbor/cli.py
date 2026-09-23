"""Командная строка: python -m podbor.cli --city Алматы --date 2026-11-14 ...

Флаг --json печатает сырой ответ API вместо человекочитаемого вида.
"""
import argparse
import json
import sys

from .data import load_vendors
from .engine import recommend


def render(res: dict) -> str:
    out = [f"[{res['outcome_label']}]", res["headline"]]
    for key in ("why_fewer", "date_effect"):
        if res.get(key):
            out.append(res[key])
    out += res.get("notes", [])
    for c in res.get("cards", []):
        flags = f" | {'; '.join(c['flags'])}" if c["flags"] else ""
        out.append(f"\n  {c['rank']}. {c['name']} — {c['category']}, {c['city']}, {c['price_text']}"
                   f"  [{c['provenance']}{flags}]")
        out.append(f"     {c['explanation']}")
    if res.get("outcome") == "no_match":
        out.append("\n  Почему не подошли:")
        for r in res.get("rejected", []):
            out.append(f"   - {r['name']}: {'; '.join(r['reasons'])}")
    for s in res.get("suggestions", []):
        out.append(f"\n  Подсказка: {s}")
    out.append(f"\n  ({res.get('elapsed_ms')} мс)")
    return "\n".join(out)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    p = argparse.ArgumentParser(description="Подбор до 3 подрядчиков с объяснением")
    p.add_argument("--city", required=True)
    p.add_argument("--date", required=True, help="ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")
    p.add_argument("--format", dest="event_format", required=True,
                   help="свадьба | той | корпоратив | конференция | юбилей | день рождения")
    p.add_argument("--category", required=True)
    p.add_argument("--budget", required=True)
    p.add_argument("--hours")
    p.add_argument("--language", help="русский | казахский | английский")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    vendors, _ = load_vendors()
    res = recommend(vendors, {k: v for k, v in vars(a).items() if k != "json"})
    print(json.dumps(res, ensure_ascii=False, indent=2) if a.json else render(res))


if __name__ == "__main__":
    main()
