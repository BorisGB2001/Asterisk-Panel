"""Interface translations: app/locales/<code>.json, key — the English phrase, value — its translation.

New language: copy en.json to <code>.json, translate the values and "_language". {name} parts are placeholders,
do not translate them. `python -m app.i18n` adds new phrases from the code and templates to en.json and shows what
is missing in the other files.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from contextvars import ContextVar
from pathlib import Path

LOCALES = Path(__file__).parent / "locales"
DEFAULT = os.environ.get("DEFAULT_LANG", "ru")

_catalogs: dict[str, dict[str, str]] = {}
_lang: ContextVar[str] = ContextVar("lang", default=DEFAULT)


def load() -> None:
    _catalogs.clear()
    for f in sorted(LOCALES.glob("*.json")):
        _catalogs[f.stem] = json.loads(f.read_text(encoding="utf-8"))


def languages() -> list[tuple[str, str]]:
    if not _catalogs:
        load()
    return [(code, cat.get("_language", code)) for code, cat in _catalogs.items()]


def normalize(code: str | None) -> str:
    if not _catalogs:
        load()
    return code if code in _catalogs else DEFAULT


def get() -> str:
    return _lang.get()


def set_lang(code: str | None) -> None:
    _lang.set(normalize(code))


@contextlib.contextmanager
def use(code: str | None):
    token = _lang.set(normalize(code))
    try:
        yield
    finally:
        _lang.reset(token)


def t(text: str, **kw) -> str:
    """Phrase in the current language; no translation — the English original."""
    if not _catalogs:
        load()
    s = _catalogs.get(_lang.get(), {}).get(text) or text
    return s.format(**kw) if kw else s


# ---- collecting phrases from code and templates ----

PATTERNS = [re.compile(r"""\bN_\(\s*"((?:[^"\\]|\\.)*)"\s*\)"""), re.compile(r"""\b_\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]"""), re.compile(r"""\bt\(\s*"((?:[^"\\]|\\.)*)"\s*[,)]"""),
            re.compile(r"""\b_\(\s*'((?:[^'\\]|\\.)*)'\s*[,)]"""), re.compile(r"""\bt\(\s*'((?:[^'\\]|\\.)*)'\s*[,)]""")]


def extract() -> set[str]:
    root = Path(__file__).parent
    found: set[str] = set()
    for f in list(root.glob("*.py")) + list((root / "templates").glob("*.html")):
        text = f.read_text(encoding="utf-8")
        for p in PATTERNS:
            found |= {m.encode().decode("unicode_escape") if "\\" in m else m for m in p.findall(text)}
    return found


def _cli() -> None:
    load()
    keys = extract()
    en = _catalogs.get("en", {"_language": "English"})
    new = sorted(k for k in keys if k not in en)
    for k in new:
        en[k] = k
    (LOCALES / "en.json").write_text(json.dumps(dict(sorted(en.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"en.json: {len(new)} added")
    for code, cat in _catalogs.items():
        if code != "en":
            miss = sorted(k for k in keys if k not in cat)
            print(f"{code}.json: {len(miss)} missing" + ("".join(f"\n  {m}" for m in miss[:50])))
    sys.exit(0)


if __name__ == "__main__":
    _cli()
