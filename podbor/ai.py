"""ИИ-агент поверх детерминированного движка подбора.

Агент понимает запрос обычным текстом («свадьба в Алматы 14 ноября, бюджет 500к,
ведущий на казахском»), сам вызывает движок как инструмент и пишет короткий совет.

Как агент устроен:
  1. LLM получает текст и справочники: города, категории, форматы, языки, окно календаря.
  2. LLM вызывает инструмент search_vendors. Параметры ограничены JSON-схемой со
     списками допустимых значений, поэтому выдумать город или категорию нельзя.
  3. Инструмент — это обычный recommend() из engine.py. Фильтры, ранжирование и
     объяснения карточек остаются детерминированными.
  4. Если подходящих мало или нет, агент может сделать ещё до двух поисков, например
     на соседнюю дату из подсказок движка.
  5. Итоговый совет проверяется: каждое число в нём должно встречаться в запросе
     пользователя или в ответах инструмента. Не прошёл проверку — показываем
     детерминированный текст движка.

Без ключа OPENAI_API_KEY агент выключен, остальной сервис работает как раньше.

Запуск из командной строки:  python -m podbor.ai "свадьба в Алматы 14 ноября, бюджет 500к"
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

from .data import WINDOW_END, WINDOW_START
from .engine import FORMATS, LANGUAGES, recommend

ROOT = Path(__file__).resolve().parent.parent
API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5.4-mini"
MAX_TOOL_CALLS = 3
MAX_TURNS = 5
TIMEOUT_S = 40


def _load_env_file() -> None:
    """Читает .env в корне проекта. Переменные окружения имеют приоритет."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()


def model_name() -> str:
    return os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)


def enabled() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def status() -> dict:
    return {"enabled": enabled(), "model": model_name() if enabled() else None}


# ---------------------------------------------------------------- LLM-вызов

def _chat(messages: list, tools: list) -> dict:
    """Один вызов Chat Completions. Возвращает message ассистента."""
    body = {"model": model_name(), "messages": messages, "tools": tools,
            "parallel_tool_calls": False}
    if model_name().startswith("gpt-5"):
        body["reasoning_effort"] = "none"  # иначе function tools в chat/completions недоступны
    else:
        body["temperature"] = 0
    req = urllib.request.Request(
        API_URL, json.dumps(body, ensure_ascii=False).encode("utf-8"),
        {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"OpenAI вернул ошибку {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"Не удалось связаться с OpenAI: {e}") from None
    return data["choices"][0]["message"]


# ---------------------------------------------------------------- Инструмент

def _tool_schema(cities: list, categories: list) -> list:
    return [{
        "type": "function",
        "function": {
            "name": "search_vendors",
            "description": ("Подбирает до трёх подрядчиков из каталога. Учитывает занятость на дату, "
                            "формат, бюджет, язык и часы. Возвращает исход, карточки с объяснениями, "
                            "причины отсева и подсказки: соседние даты и нужный бюджет."),
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["city", "date", "event_format", "category", "budget", "hours", "language"],
                "properties": {
                    "city": {"type": "string", "enum": cities},
                    "date": {"type": "string", "description": "ГГГГ-ММ-ДД"},
                    "event_format": {"type": "string", "enum": FORMATS},
                    "category": {"type": "string", "enum": categories},
                    "budget": {"type": "number", "description": "бюджет в тенге на этого подрядчика"},
                    "hours": {"type": ["number", "null"], "description": "длительность, если названа"},
                    "language": {"type": ["string", "null"], "enum": LANGUAGES + [None]},
                },
            },
        },
    }]


def _system_prompt(cities: list, categories: list) -> str:
    today = date(2026, 9, 23) if date.today() < WINDOW_START else date.today()
    return f"""Ты ассистент сервиса подбора event-подрядчиков в Казахстане. Отвечай по-русски.
Сегодня {today.isoformat()}. Календари занятости известны с {WINDOW_START.isoformat()} по {WINDOW_END.isoformat()}.

Порядок работы:
1. Извлеки из запроса город, дату, тип события, категорию подрядчика, бюджет в тенге, а также часы и язык, если они названы.
   «500к» и «500 тысяч» — это 500000, «1.2 млн» — 1200000. Дату без года относи к ближайшей будущей.
   Если пользователь называет роль словами («тамада», «фотограф на свадьбу», «ресторан»), выбери ближайшую категорию из списка.
2. Вызови search_vendors ровно с тем, что просил пользователь. Если не хватает города, даты, типа события, категории или бюджета,
   не выдумывай: коротко спроси недостающее одним вопросом и не вызывай инструмент.
3. Если исход no_match или подрядчиков меньше трёх, обязательно сделай ещё один поиск, а при необходимости второй.
   Меняй ровно один параметр по подсказкам из suggestions: сначала ближайшая соседняя дата, где кто-то подходит,
   иначе бюджет, который там назван. Не меняй город, категорию и тип события.
   Бюджет пользователя — тот, что он назвал сам. Вариант из альтернативного поиска никогда не называй
   «в вашем бюджете» или «в вашу дату», если бюджет или дата в нём другие: прямо скажи, насколько он дороже или на какую дату.
4. В конце напиши совет из 2–4 предложений от лица сервиса («советуем», «подойдёт»): кого выбрать из первого поиска и почему,
   а если искал альтернативы — что именно они дали. Используй только имена, цены и даты из ответов инструмента.
   Не придумывай фактов. Не задавай вопросов и не предлагай сделать что-то ещё. Без markdown и списков.

Города: {", ".join(cities)}.
Категории: {", ".join(categories)}.
Типы событий: {", ".join(FORMATS)}. Языки: {", ".join(LANGUAGES)}."""


def _compact(res: dict) -> dict:
    """Сжатый ответ движка для LLM: только то, что нужно для совета."""
    return {
        "outcome": res.get("outcome"),
        "headline": res.get("headline"),
        "why_fewer": res.get("why_fewer"),
        "date_effect": res.get("date_effect"),
        "cards": [{"rank": c["rank"], "name": c["name"], "price_from_kzt": c["price_from_kzt"],
                   "price_text": c["price_text"], "languages": c["languages"],
                   "explanation": c["explanation"]} for c in res.get("cards", [])],
        "rejected": [{"name": r["name"], "reasons": r["reasons"]} for r in res.get("rejected", [])][:8],
        "suggestions": res.get("suggestions", []),
    }


_LABELS = {"date": "дата", "budget": "бюджет", "hours": "часы", "language": "язык",
           "city": "город", "category": "категория", "event_format": "тип события"}


def _diff_note(user: dict, alt: dict) -> str:
    """Явная пометка для модели: чем альтернативный поиск отличается от запроса пользователя."""
    diffs = [f"{_LABELS.get(k, k)} {alt.get(k, 'не указан')} вместо {user.get(k, 'не указан')}"
             for k in _LABELS if alt.get(k) != user.get(k)]
    if not diffs:
        return "Повтор того же поиска, условия пользователя не изменены."
    return ("Это альтернатива, а не исходный запрос пользователя. Отличия: " + "; ".join(diffs)
            + ". В совете прямо скажи, что вариант выходит за исходные условия.")


# ---------------------------------------------------------------- Проверка фактов

_NUM = re.compile(r"\d[\d\s  .,]*\d|\d")


def _numbers(text: str) -> set:
    out = set()
    for m in _NUM.finditer(text or ""):
        raw = re.sub(r"[\s  ]", "", m.group())
        for part in re.split(r"[.,]", raw):
            if part:
                out.add(part.lstrip("0") or "0")
        out.add(re.sub(r"[.,]", "", raw).lstrip("0") or "0")
    return out


def fact_check(answer: str, sources: list) -> list:
    """Числа из ответа, которых нет ни в одном источнике. Пустой список — проверка пройдена."""
    allowed = set()
    for s in sources:
        allowed |= _numbers(s if isinstance(s, str) else json.dumps(s, ensure_ascii=False))
    bad = []
    for m in _NUM.finditer(answer or ""):
        raw = re.sub(r"[\s  ]", "", m.group()).strip(".,")
        full = re.sub(r"[.,]", "", raw).lstrip("0") or "0"
        parts = [p.lstrip("0") or "0" for p in re.split(r"[.,]", raw) if p]
        if full not in allowed and not all(p in allowed for p in parts):
            bad.append(m.group().strip())
    return bad


# ---------------------------------------------------------------- Агент

def ask(vendors: list, text: str, cities: list, categories: list, chat=None) -> dict:
    """Запускает агента на запросе обычным текстом. chat подменяется в тестах."""
    t0 = time.perf_counter()
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "Опишите событие хотя бы одной фразой."}
    if chat is None:
        if not enabled():
            return {"ok": False, "error": "ИИ выключен: не задан OPENAI_API_KEY. Форма подбора работает без него."}
        chat = _chat
    tools = _tool_schema(cities, categories)
    messages = [{"role": "system", "content": _system_prompt(cities, categories)},
                {"role": "user", "content": text}]
    steps, sources, first = [], [text], None

    try:
        for _ in range(MAX_TURNS):
            msg = chat(messages, tools)
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = (msg.get("content") or "").strip()
                break
            messages.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            for call in calls:
                if len(steps) >= MAX_TOOL_CALLS:
                    payload = {"error": "лимит поисков исчерпан, напиши итоговый совет"}
                else:
                    try:
                        params = json.loads(call["function"]["arguments"] or "{}")
                    except json.JSONDecodeError:
                        params = {}
                    params = {k: v for k, v in params.items() if v is not None}
                    res = recommend(vendors, params)
                    payload = _compact(res)
                    if first is not None:
                        payload = {"alternative_search": _diff_note(first[0], params), **payload}
                    steps.append({"params": params, "outcome": res["outcome"],
                                  "outcome_label": res["outcome_label"], "headline": res["headline"],
                                  "found": len(res.get("cards", []))})
                    sources.append(payload)
                    sources.append(res.get("request") or {})
                    if first is None:
                        first = (params, res)
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(payload, ensure_ascii=False)})
        else:
            answer = ""
    except RuntimeError as e:
        return {"ok": False, "error": str(e), "steps": steps}

    elapsed = round((time.perf_counter() - t0) * 1000)
    base = {"ok": True, "model": model_name(), "steps": steps, "elapsed_ms": elapsed}
    if first is None:
        return {**base, "clarify": answer or "Уточните город, дату, тип события, категорию и бюджет."}

    params, res = first
    unverified = fact_check(answer, sources)
    verified = bool(answer) and not unverified
    return {**base, "params": params, "result": res,
            "answer": answer if verified else res["headline"],
            "verified": verified, "unverified": unverified}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print('Использование: python -m podbor.ai "свадьба в Алматы 14 ноября, бюджет 500к"')
        return 2
    from .data import load_vendors, norm
    vendors, _ = load_vendors()
    cities = sorted({v.city for v in vendors if v.city})
    cats = {}
    for v in vendors:
        for c in v.categories:
            cats.setdefault(norm(c), c)
    out = ask(vendors, " ".join(argv), cities, sorted(cats.values()))
    if not out["ok"]:
        print(out["error"])
        return 1
    for i, s in enumerate(out["steps"], 1):
        print(f"Шаг {i}: search_vendors({json.dumps(s['params'], ensure_ascii=False)}) → {s['outcome_label']}")
    if "clarify" in out:
        print("\nИИ уточняет:", out["clarify"])
        return 0
    from .cli import render
    print("\n" + render(out["result"]))
    print(f"\nСовет ИИ ({'проверен' if out['verified'] else 'не прошёл проверку фактов, показан текст движка'}):")
    print(out["answer"])
    print(f"({out['elapsed_ms']} мс, модель {out['model']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
