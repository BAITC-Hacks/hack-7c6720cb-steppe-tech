"""Прогон обязательных сценариев из Definition of Done.

Сценарии подбираются автоматически по загруженным данным (podbor/scenarios.py),
поэтому скрипт работает и на демо-наборе, и на настоящем датасете:

  1. плотная категория на осеннюю дату и тот же запрос на другую дату
     (выдача отличается, и в тексте видно, что дело в занятости);
  2. редкая категория;
  3. в городе нет такой категории;
  4. кандидаты есть, но ни один не проходит;
  5. детерминизм: каждый сценарий 5 раз подряд.

    python scripts/demo.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from podbor.cli import render  # noqa: E402
from podbor.data import load_vendors  # noqa: E402
from podbor.engine import recommend  # noqa: E402
from podbor.scenarios import find_all  # noqa: E402


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    vendors, info = load_vendors()
    print(f"Данные: {info['file']}, профилей {info['total']}, синтетических {info['synthetic']}")
    scenarios = find_all(vendors)
    results = {}
    for i, sc in enumerate(scenarios, 1):
        print("\n" + "=" * 100 + f"\n{i}. {sc['title']}\n" + "=" * 100)
        print("Запрос:", ", ".join(f"{k}={v}" for k, v in sc["params"].items() if v))
        res = recommend(vendors, sc["params"])
        results[sc["key"]] = res
        print(render(res))

    if "dense" in results and "dense_other_date" in results:
        a = {c["name"] for c in results["dense"]["cards"]}
        b = {c["name"] for c in results["dense_other_date"]["cards"]}
        da, db = results["dense"]["request"]["date"], results["dense_other_date"]["request"]["date"]
        print(f"\nРазница выдач по датам: только {da}: {sorted(a - b)}; только {db}: {sorted(b - a)}")

    print("\n" + "=" * 100 + "\nДетерминизм: каждый сценарий 5 раз подряд\n" + "=" * 100)
    ok = True
    for sc in scenarios:
        runs = {tuple(c["id"] for c in recommend(vendors, sc["params"])["cards"]) for _ in range(5)}
        ok &= len(runs) == 1
        print(f"  {sc['key']:<18} уникальных порядков: {len(runs)}")
    print("OK" if ok else "FAIL")


if __name__ == "__main__":
    main()
