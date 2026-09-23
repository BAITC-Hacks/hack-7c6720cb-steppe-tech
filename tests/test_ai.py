"""Тесты ИИ-агента. Модель подменяется сценарием, поэтому сеть и ключ не нужны."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from podbor import ai  # noqa: E402
from podbor.data import load_vendors, norm  # noqa: E402
from podbor.scenarios import find_all  # noqa: E402

VENDORS, _ = load_vendors()
CITIES = sorted({v.city for v in VENDORS if v.city})
CATEGORIES = sorted({norm(c): c for v in VENDORS for c in v.categories}.values())
DENSE = find_all(VENDORS)[0]["params"]


def tool_call(params, cid="c1"):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": cid, "type": "function",
         "function": {"name": "search_vendors", "arguments": json.dumps(params, ensure_ascii=False)}}]}


def scripted(*replies):
    """Фейковая модель: отдаёт ответы по порядку и запоминает, что ей прислали."""
    seen = []

    def chat(messages, tools):
        seen.append([dict(m) for m in messages])
        return replies[len(seen) - 1]
    chat.seen = seen
    return chat


class FactCheckTest(unittest.TestCase):
    def test_numbers_from_sources_pass(self):
        src = [{"price_text": "от 500 000 ₸", "headline": "На 26.09.2026 (сб) заняты 6 из 10."}]
        self.assertEqual(ai.fact_check("Советуем: от 500 000 ₸, 26.09.2026, заняты 6 из 10.", src), [])

    def test_invented_number_fails(self):
        src = [{"price_text": "от 500 000 ₸"}]
        self.assertEqual(ai.fact_check("Цена от 450 000 ₸.", src), ["450 000"])

    def test_text_without_numbers_passes(self):
        self.assertEqual(ai.fact_check("Советуем первого из подборки.", []), [])


class AgentTest(unittest.TestCase):
    def test_disabled_without_key(self):
        old = os.environ.pop("OPENAI_API_KEY", None)
        try:
            out = ai.ask(VENDORS, "свадьба", CITIES, CATEGORIES)
            self.assertFalse(out["ok"])
            self.assertIn("OPENAI_API_KEY", out["error"])
        finally:
            if old is not None:
                os.environ["OPENAI_API_KEY"] = old

    def test_tool_result_is_engine_result(self):
        from podbor.engine import recommend
        expected = recommend(VENDORS, DENSE)
        name = expected["cards"][0]["name"]
        chat = scripted(tool_call(DENSE), {"role": "assistant", "content": f"Советуем «{name}»."})
        out = ai.ask(VENDORS, "запрос", CITIES, CATEGORIES, chat=chat)
        self.assertTrue(out["ok"])
        self.assertEqual([c["id"] for c in out["result"]["cards"]], [c["id"] for c in expected["cards"]])
        self.assertTrue(out["verified"])
        self.assertEqual(len(out["steps"]), 1)
        # движку отправлен ответ инструмента, модели он вернулся вторым вызовом
        self.assertEqual(chat.seen[1][-1]["role"], "tool")

    def test_hallucinated_answer_replaced_by_engine_text(self):
        chat = scripted(tool_call(DENSE), {"role": "assistant", "content": "Советуем, всего 123 456 ₸."})
        out = ai.ask(VENDORS, "запрос", CITIES, CATEGORIES, chat=chat)
        self.assertFalse(out["verified"])
        self.assertEqual(out["answer"], out["result"]["headline"])

    def test_clarification_without_tool_call(self):
        chat = scripted({"role": "assistant", "content": "В каком городе и на какую дату?"})
        out = ai.ask(VENDORS, "нужен фотограф", CITIES, CATEGORIES, chat=chat)
        self.assertEqual(out["clarify"], "В каком городе и на какую дату?")
        self.assertNotIn("result", out)

    def test_tool_call_limit(self):
        calls = [tool_call(DENSE, f"c{i}") for i in range(ai.MAX_TURNS)]
        out = ai.ask(VENDORS, "запрос", CITIES, CATEGORIES, chat=scripted(*calls))
        self.assertEqual(len(out["steps"]), ai.MAX_TOOL_CALLS)

    def test_schema_limits_values_to_catalog(self):
        props = ai._tool_schema(CITIES, CATEGORIES)[0]["function"]["parameters"]["properties"]
        self.assertEqual(props["city"]["enum"], CITIES)
        self.assertEqual(props["category"]["enum"], CATEGORIES)


if __name__ == "__main__":
    unittest.main()
