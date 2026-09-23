"""Проверки требований задачи на маленьком ручном наборе профилей.

    python -m unittest discover -s tests -v
"""
import datetime as dt
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from podbor.data import _vendor, load_vendors  # noqa: E402
from podbor.engine import recommend  # noqa: E402
from podbor.scenarios import find_all  # noqa: E402

SAT = "2026-11-14"
SUN = "2026-11-15"


def row(i, **kw):
    base = {
        "id": f"t{i:02d}", "anon_name": f"Профиль {i}", "categories": ["Ведущий"], "city": "Алматы",
        "price_from_kzt": 200_000, "event_formats": ["свадьба", "той", "корпоратив"],
        "languages": ["русский", "казахский"], "max_hours": 6, "busy_dates": [],
        "description": "Веду мероприятия. Опыт 5 лет.", "synthetic": False,
        "city_imputed": False, "price_imputed": False,
    }
    base.update(kw)
    return _vendor(base, "dataset")


VENDORS = [
    row(1, description="Веду свадьбы с 2012 года. Работаю в паре с диджеем.", price_from_kzt=300_000),
    row(2, description="Корпоративы и тимбилдинги для компаний. Опыт 8 лет.", price_from_kzt=150_000),
    row(3, description="Провожу тои и узату, знаю обряды беташар.", busy_dates=[SAT], price_from_kzt=250_000),
    row(4, price_from_kzt=900_000, description="Премиальные свадьбы на 500 гостей."),
    row(5, event_formats=["конференция"], description="Модерирую конференции."),
    row(6, languages=["русский"], max_hours=3, price_from_kzt=180_000, description="Свадьбы и юбилеи."),
    row(7, categories=["Флорист"], max_hours=None, busy_dates=[SAT], description="Букеты невесты."),
    row(8, categories=["Флорист"], max_hours=None, price_from_kzt=600_000, description="Оформление свадеб цветами."),
    row(9, categories=["Отель"], city="Астана", max_hours=None, synthetic=True),
]


def ask(**kw):
    p = {"city": "Алматы", "date": SAT, "event_format": "свадьба", "category": "Ведущий", "budget": 500_000}
    p.update(kw)
    return recommend(VENDORS, p)


class Outcomes(unittest.TestCase):
    def test_found_at_most_three(self):
        r = ask()
        self.assertEqual(r["outcome"], "found")
        self.assertLessEqual(len(r["cards"]), 3)

    def test_busy_vendor_never_shown(self):
        r = ask(event_format="той")
        self.assertNotIn("t03", [c["id"] for c in r["cards"]])
        r2 = ask(event_format="той", date=SUN)
        self.assertIn("t03", [c["id"] for c in r2["cards"]])

    def test_venue_category_uses_same_calendar(self):
        r = ask(category="Флорист", budget=1_000_000)
        self.assertNotIn("t07", [c["id"] for c in r["cards"]])

    def test_no_category_in_city(self):
        r = ask(city="Астана", category="Флорист")
        self.assertEqual(r["outcome"], "no_category_in_city")
        self.assertIn("Алматы", r["headline"])
        self.assertEqual(r["cards"], [])

    def test_no_match_is_explained(self):
        r = ask(category="Флорист", budget=300_000)  # t07 занят, t08 дороже
        self.assertEqual(r["outcome"], "no_match")
        self.assertIn("занят", r["headline"])
        self.assertIn("дороже бюджета", r["headline"])
        reasons = {x["id"]: " ".join(x["reasons"]) for x in r["rejected"]}
        self.assertIn("занят", reasons["t07"])
        self.assertIn("выше бюджета", reasons["t08"])
        self.assertTrue(r["suggestions"])

    def test_fewer_than_three_says_why(self):
        r = ask(event_format="конференция")
        self.assertEqual(len(r["cards"]), 1)
        self.assertTrue(r["why_fewer"])
        self.assertIn("не берут формат", r["why_fewer"])

    def test_invalid_request(self):
        r = ask(budget="abc", event_format="пикник")
        self.assertEqual(r["outcome"], "invalid_request")
        self.assertIn("бюджет", r["headline"])

    def test_hard_filters(self):
        ids = [c["id"] for c in ask(budget=500_000)["cards"]]
        self.assertNotIn("t04", ids)  # дороже бюджета
        ids = [c["id"] for c in ask(language="казахский")["cards"]]
        self.assertNotIn("t06", ids)  # нет казахского
        ids = [c["id"] for c in ask(hours=5)["cards"]]
        self.assertNotIn("t06", ids)  # максимум 3 ч


class Explanations(unittest.TestCase):
    def test_deterministic(self):
        runs = {tuple((c["id"], c["explanation"]) for c in ask()["cards"]) for _ in range(10)}
        self.assertEqual(len(runs), 1)

    def test_not_interchangeable(self):
        cards = ask()["cards"]
        self.assertGreaterEqual(len(cards), 2)
        texts = [c["explanation"].replace(c["name"], "") for c in cards]
        self.assertEqual(len(set(texts)), len(texts))

    def test_one_or_two_sentences_with_facts(self):
        for c in ask()["cards"]:
            text = re.sub(r"«[^»]*»", "«»", c["explanation"])
            self.assertLessEqual(text.count(". ") + 1, 2, c["explanation"])
            self.assertIn("₸", c["explanation"])
            self.assertNotIn("отличный выбор", c["explanation"].lower())

    def test_quote_from_description(self):
        card = next(c for c in ask()["cards"] if c["id"] == "t01")
        self.assertIn("Веду свадьбы с 2012 года", card["explanation"])

    def test_date_changes_result_and_says_so(self):
        a = ask(event_format="той")
        b = ask(event_format="той", date=SUN)
        self.assertNotEqual([c["id"] for c in a["cards"]], [c["id"] for c in b["cards"]])
        self.assertIn("Профиль 3", a["date_effect"])
        self.assertIn("занят", a["date_effect"])


class OnLoadedData(unittest.TestCase):
    """Те же обязательные сценарии на данных из data/ (демо или настоящих)."""

    @classmethod
    def setUpClass(cls):
        cls.vendors, _ = load_vendors()
        cls.sc = {s["key"]: s["params"] for s in find_all(cls.vendors)}

    def test_all_outcomes_present(self):
        for key in ("dense", "rare", "no_category", "no_match"):
            self.assertIn(key, self.sc)

    def test_fast_enough(self):
        t = dt.datetime.now()
        for p in self.sc.values():
            recommend(self.vendors, p)
        self.assertLess((dt.datetime.now() - t).total_seconds(), 2)


class CsvFromExcel(unittest.TestCase):
    """CSV, как его сохраняет русский Excel: «;», кириллица в заголовках, даты через перенос в ячейке."""

    def _load(self, encoding):
        import tempfile
        text = ("Название;Категория;Город;Цена от;Форматы;Языки;Макс часов;Занятые даты;Описание;Синтетический\r\n"
                "Тест А.;Ведущий;Алматы;200 000;свадьба, той;русский, казахский;6;\"14.11.2026\r\n15.11.2026\";"
                "Веду свадьбы.;ЛОЖЬ\r\n"
                "Тест Б.;Ведущий;Алматы;150000;свадьба;русский;;;Свадьбы и юбилеи.;ИСТИНА\r\n")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "база.csv"
            path.write_bytes(text.encode(encoding))
            return load_vendors(path)[0]

    def test_utf8_and_cp1251(self):
        for enc in ("utf-8-sig", "cp1251"):
            a, b = self._load(enc)
            self.assertEqual(a.name, "Тест А.")
            self.assertEqual(a.price, 200_000)
            self.assertEqual(a.formats, ["свадьба", "той"])
            self.assertEqual(a.busy, frozenset({dt.date(2026, 11, 14), dt.date(2026, 11, 15)}))
            self.assertFalse(a.synthetic)
            self.assertTrue(b.synthetic)
            self.assertIsNone(b.max_hours)

    def test_missing_columns_explained(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.csv"
            path.write_text("Имя;Телефон\r\nА;1\r\n", encoding="utf-8")
            with self.assertRaises(ValueError) as e:
                load_vendors(path)
            self.assertIn("не найдены столбцы", str(e.exception))


if __name__ == "__main__":
    unittest.main()
