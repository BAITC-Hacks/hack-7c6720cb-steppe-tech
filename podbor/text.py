"""Мелкие помощники для русского текста: числа, деньги, даты, склонения."""
import datetime as dt

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

CITY_IN = {"алматы": "в Алматы", "астана": "в Астане", "зарубежье": "за рубежом"}
LANG_ON = {"русский": "на русском", "казахский": "на казахском", "английский": "на английском"}
LANG_STEM = {"русский": "русск", "казахский": "казах", "английский": "англ"}
FORMAT_PLURAL = {"свадьба": "свадьбы", "той": "тои", "корпоратив": "корпоративы",
                 "конференция": "конференции", "юбилей": "юбилеи", "день рождения": "дни рождения"}


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def money(x) -> str:
    if x is None:
        return "—"
    return f"{x:,}".replace(",", " ") + " ₸"


def ddate(d: dt.date, weekday: bool = False) -> str:
    s = d.strftime("%d.%m")
    return f"{s} ({WEEKDAYS[d.weekday()]})" if weekday else s


def ddate_full(d: dt.date) -> str:
    return f"{d.strftime('%d.%m.%Y')} ({WEEKDAYS[d.weekday()]})"


def city_in(city: str) -> str:
    from .data import norm
    return CITY_IN.get(norm(city), f"в городе {city}")


def hours(h: float) -> str:
    return f"{h:g} ч"


def cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def join_ru(items: list) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " и " + items[-1]


def quoted(name: str) -> str:
    """Имя в «ёлочках», если в нём своих кавычек нет (иначе «Зал «Жібек»» выглядит сломанным)."""
    return name if "«" in name else f"«{name}»"
