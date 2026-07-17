"""UI translation helpers.

Translation files live in this directory as ``en.yaml``, ``et.yaml``, etc.
The active language is read from the ``mr_lang`` cookie (set by ui_prefs.js),
falling back to the best match in the Accept-Language header, then English.

Usage in routes::

    from ..i18n import get_t, resolve_lang
    lang = resolve_lang(request)
    t = get_t(lang)
    # pass t=t, lang=lang into template context

Usage in templates::

    {{ t.nav.devices }}
    {{ t.device.detail.delete_confirm | replace('{name}', device.name) }}
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

_I18N_DIR = Path(__file__).parent
SUPPORTED_LANGS = ("en", "et", "de", "es", "fr", "zh", "fi", "ja", "pt", "hi", "uk", "pl", "it")


@cache
def _all_translations() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for p in sorted(_I18N_DIR.glob("*.yaml")):
        with p.open(encoding="utf-8") as f:
            result[p.stem] = yaml.safe_load(f) or {}
    return result


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


class T:
    """Nested dict wrapper enabling ``t.section.key`` dot-access in Jinja2."""

    __slots__ = ("_data",)

    def __init__(self, data: dict[str, Any]) -> None:
        object.__setattr__(self, "_data", data)

    def __getattr__(self, key: str) -> Any:
        data: dict[str, Any] = object.__getattribute__(self, "_data")
        val = data.get(key)
        if val is None:
            return ""
        if isinstance(val, dict):
            return T(val)
        return val

    def __str__(self) -> str:
        data: dict[str, Any] = object.__getattribute__(self, "_data")
        if isinstance(data, dict):
            return ""
        return str(data)

    def __html__(self) -> str:  # Jinja2 respects this for auto-escaping
        return self.__str__()


@cache
def get_t(lang: str) -> T:
    """Return a T for *lang*, with English as fallback for missing keys."""
    trans = _all_translations()
    en = trans.get("en", {})
    if lang not in trans or lang == "en":
        return T(en)
    return T(_deep_merge(en, trans[lang]))


def _parse_accept_language(accept_language: str) -> list[str]:
    """Split an Accept-Language header into tags ordered by descending q weight."""
    parsed: list[tuple[str, float]] = []
    for seg in accept_language.split(","):
        seg = seg.strip()
        if not seg:
            continue
        tag, _, params = seg.partition(";")
        q = 1.0
        for param in params.split(";"):
            param = param.strip()
            if param.startswith("q="):
                try:
                    q = float(param[2:])
                except ValueError:
                    q = 0.0
        parsed.append((tag.strip().lower(), q))
    parsed.sort(key=lambda item: item[1], reverse=True)
    return [tag for tag, _ in parsed]


def resolve_lang(cookie: str, accept_language: str = "") -> str:
    """Pick the best supported language from cookie → Accept-Language → 'en'."""
    if cookie in SUPPORTED_LANGS:
        return cookie
    for tag in _parse_accept_language(accept_language):
        if tag in SUPPORTED_LANGS:
            return tag
        short = tag.split("-")[0]
        if short in SUPPORTED_LANGS:
            return short
    return "en"
