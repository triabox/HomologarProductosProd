"""Normalización de valores antes de comparar.

CoRD y VTEX comparten en su mayoría las mismas etiquetas de especificación
(ej. "Memoria RAM"), por lo que el mapeo de atributos es casi identidad: se normaliza
la etiqueta (minúsculas, sin acentos, sin espacios extra) para emparejar y se comparan
los valores normalizados. Casos especiales se agregan en ATTR_ALIASES.
"""
from __future__ import annotations

import re
import unicodedata

# Alias de etiquetas de atributos: clave normalizada CoRD -> clave normalizada VTEX.
# Vacío por ahora (las etiquetas coinciden); se completa si Etapa 0/uso detecta diferencias.
ATTR_ALIASES: dict[str, str] = {}


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def norm_text(text: str | None) -> str:
    """Normaliza texto para comparación: minúsculas, sin acentos, espacios colapsados."""
    if not text:
        return ""
    t = strip_accents(text.lower())
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def norm_label(label: str) -> str:
    """Normaliza la etiqueta de un atributo (sin puntuación) y aplica alias.

    Quita signos como ':' para que 'Contenido:' (VTEX) y 'Contenido' (CoRD) matcheen.
    """
    key = norm_text(label)
    key = re.sub(r"[^\w\s]", " ", key)       # ':', '.', '-', etc. -> espacio
    key = re.sub(r"\s+", " ", key).strip()
    return ATTR_ALIASES.get(key, key)


def is_meaningful_label(label: str) -> bool:
    """True si la etiqueta parece un atributo real (tiene al menos una letra).

    Descarta labels que son solo códigos numéricos (ej. '3090368', '630717'),
    que no son atributos visibles para el cliente.
    """
    return any(c.isalpha() for c in (label or ""))


def norm_attr_value(value: str | None) -> str:
    """Normaliza el valor de un atributo (texto + unidades/comillas comunes)."""
    t = norm_text(value)
    t = t.replace('"', "").replace("”", "").replace("''", "")
    t = re.sub(r"\s*(gb|mb|mah|mp|mpx|kg|gr|g|cm|mm|pulgadas|hz)\b", r" \1", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_price(value: float | None) -> float | None:
    """Redondea el precio a 2 decimales para comparación robusta."""
    if value is None:
        return None
    return round(float(value), 2)


def slugify(text: str) -> str:
    """Convierte un nombre de categoría en slug de URL (sin acentos, kebab-case)."""
    t = strip_accents(text.lower())
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-")


def slugify_path(names: list[str]) -> str:
    """Path de slugs separado por '/', ej. ['Tecnologia','Telefonia'] -> 'tecnologia/telefonia'."""
    return "/".join(slugify(n) for n in names)


def norm_attributes(attrs: dict[str, str]) -> dict[str, str]:
    """Devuelve {label_normalizada: value_normalizado}, descartando labels no relevantes."""
    out: dict[str, str] = {}
    for label, value in attrs.items():
        if not is_meaningful_label(label):   # descartar códigos numéricos
            continue
        key = norm_label(label)
        if key:
            out[key] = norm_attr_value(value)
    return out
