"""Чтение первого листа .xlsx без внешних библиотек.

Файл .xlsx — это zip-архив с XML. Берём первый лист, первую строку считаем
заголовками, остальные строки превращаем в словари {заголовок: значение}.
Все значения возвращаются строками: дальше их разбирает загрузчик в data.py.
"""
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "rel": "http://schemas.openxmlformats.org/package/2006/relationships"}


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref).group(0)
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _text(el) -> str:
    """Текст ячейки или общей строки, включая форматированные куски <r><t>."""
    return "".join(t.text or "" for t in el.iter(f"{{{NS['m']}}}t"))


def _first_sheet_path(z: zipfile.ZipFile) -> str:
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheet = wb.find("m:sheets/m:sheet", NS)
    rid = sheet.get(f"{{{NS['r']}}}id")
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.findall("rel:Relationship", NS):
        if rel.get("Id") == rid:
            target = rel.get("Target").lstrip("/")
            return target if target.startswith("xl/") else "xl/" + target
    return "xl/worksheets/sheet1.xml"


def read_xlsx(path: Path) -> list[dict]:
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = [_text(si) for si in sst.findall("m:si", NS)]
        sheet = ET.fromstring(z.read(_first_sheet_path(z)))

    rows = []
    for row in sheet.iter(f"{{{NS['m']}}}row"):
        values = {}
        for pos, c in enumerate(row.findall("m:c", NS)):
            idx = _col_index(c.get("r")) if c.get("r") else pos
            t = c.get("t")
            v = c.find("m:v", NS)
            if t == "s":
                val = shared[int(v.text)] if v is not None else ""
            elif t == "inlineStr":
                val = _text(c)
            elif t == "b":
                val = "true" if v is not None and v.text == "1" else "false"
            else:
                val = v.text if v is not None and v.text is not None else ""
                if re.fullmatch(r"-?\d+\.0+", val):
                    val = val.split(".")[0]  # 190000.0 -> 190000
            values[idx] = val
        if values:
            width = max(values) + 1
            rows.append([values.get(i, "") for i in range(width)])

    if not rows:
        return []
    header = [str(h).strip() for h in rows[0]]
    out = []
    for r in rows[1:]:
        if not any(str(x).strip() for x in r):
            continue  # пустая строка
        out.append({h: (r[i] if i < len(r) else "") for i, h in enumerate(header) if h})
    return out
