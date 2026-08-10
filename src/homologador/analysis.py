"""Análisis de causas raíz sobre las discrepancias abiertas.

- Reglas de promoción sospechosas: agrupa los fallos de precio por el % de descuento
  (redondo) que aplica cada sistema. Un grupo con varios SKUs no es ruido por producto:
  es una REGLA de promoción desincronizada (ej. "25% off activo en VTEX, ausente en CoRD").
- Impacto comercial: cruza cada discrepancia con el ranking local de Top Ventas
  (data/homologador-topventas.db — solo existe en local, nunca se sube) para priorizar
  por venta real y no solo por magnitud del desvío.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Optional

from .config import Config
from .cord_scraper import permalink_from_url
from .storage import Storage

_PRICE_FIELDS = ("precio_venta", "precio_promocional", "precio_sip")
_FIELD_NOUN = {
    "precio_venta": "Precio de venta",
    "precio_promocional": "Promoción",
    "precio_sip": "Precio tarjeta SIP",
}


# -- impacto comercial (ranking de ventas, solo local) ------------------------

def load_sales_index(cfg: Config) -> dict:
    """{permalink -> {rank, sku}} de los top de venta publicados en CoRD.

    Vacío si no existe la base local de Top Ventas (ej. en producción).
    """
    db_file = cfg.root / "data" / "homologador-topventas.db"
    if not db_file.exists():
        return {}
    con = sqlite3.connect(db_file)
    con.row_factory = sqlite3.Row
    idx: dict = {}
    try:
        rows = con.execute(
            """WITH last AS (SELECT c.*, ROW_NUMBER() OVER
                 (PARTITION BY c.sku ORDER BY c.checked_at DESC) rn FROM checks c)
               SELECT s.sku, s.rank, l.cord_url
               FROM skus s JOIN last l ON l.sku=s.sku AND l.rn=1
               WHERE l.published=1 AND l.cord_url IS NOT NULL"""
        ).fetchall()
        for r in rows:
            perma = permalink_from_url(r["cord_url"] or "")
            if not perma:
                continue
            prev = idx.get(perma)
            if prev is None or (r["rank"] or 10**9) < prev["rank"]:
                idx[perma] = {"rank": r["rank"] or 10**9, "sku": r["sku"]}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()
    return idx


def rank_for(sales_idx: dict, cord_url: Optional[str]) -> Optional[int]:
    """Ranking de venta del producto (menor = vende más), o None si no es top."""
    if not sales_idx or not cord_url:
        return None
    perma = permalink_from_url(cord_url)
    entry = sales_idx.get(perma) if perma else None
    return entry["rank"] if entry else None


def sort_by_impact(rows: list[dict]) -> None:
    """Ordena in place: top ventas primero (mejor ranking arriba), resto por peor score."""
    rows.sort(key=lambda it: (
        it.get("top_rank") is None,
        it.get("top_rank") or 0,
        it.get("score") if it.get("score") is not None else 0,
    ))


# -- detector de reglas de promoción -----------------------------------------

def _price(s) -> Optional[float]:
    if s in (None, "", "None"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _round_pct(x: float, tol: float = 0.75) -> Optional[int]:
    """Redondea un % a entero solo si está cerca de uno (señal de regla, no de ruido)."""
    r = round(x)
    return int(r) if abs(x - r) <= tol else None


def _rule_text(field: str, cord_disc: int, vtex_disc: Optional[int]) -> str:
    noun = _FIELD_NOUN.get(field, field)
    if field == "precio_venta":
        side = "más caro" if cord_disc > 0 else "más barato"
        return f"{noun} {abs(cord_disc)}% {side} en CoRD"
    if cord_disc == 0 and vtex_disc:
        return f"{noun} de {vtex_disc}% activa en VTEX, ausente en CoRD"
    if vtex_disc == 0 and cord_disc:
        return f"{noun} de {cord_disc}% activa en CoRD, no existe en VTEX"
    return f"{noun}: CoRD aplica {cord_disc}% y VTEX {vtex_disc}%"


def promo_rules(storage: Storage, sales_idx: dict, min_group: int = 3) -> list[dict]:
    """Agrupa las discrepancias de precio ABIERTAS por firma de descuento.

    Devuelve grupos con >= min_group SKUs: cada uno es una regla sospechosa,
    con desglose por categoría, top ventas afectados y SKUs de ejemplo.
    """
    open_rows = storage.open_discrepancies(_PRICE_FIELDS)
    if not open_rows:
        return []
    regs = storage.latest_regular_prices()
    groups: dict[tuple, dict] = {}
    for d in open_rows:
        c, v = _price(d["cord_value"]), _price(d["vtex_value"])
        if d["field"] == "precio_venta":
            if c is None or v is None or not v:
                continue
            pct = _round_pct((c - v) / v * 100)
            if not pct:
                continue
            key = (d["field"], pct, None)
        else:
            reg_c, reg_v = (regs.get(d["sku"]) or (None, None))
            reg_c, reg_v = _price(reg_c), _price(reg_v)
            # el precio de venta coincide ~99%: un lado puede suplir al otro
            reg_c, reg_v = (reg_c or reg_v), (reg_v or reg_c)
            if not reg_c or not reg_v:
                continue
            cord_disc = 0 if c is None else _round_pct((1 - c / reg_c) * 100)
            vtex_disc = 0 if v is None else _round_pct((1 - v / reg_v) * 100)
            if cord_disc is None or vtex_disc is None or cord_disc == vtex_disc:
                continue
            key = (d["field"], cord_disc, vtex_disc)
        g = groups.setdefault(key, {
            "field": d["field"], "cord_disc": key[1], "vtex_disc": key[2],
            "skus": [], "cats": Counter(), "top_count": 0, "top_best": None,
        })
        rank = rank_for(sales_idx, d["cord_url"])
        if rank is not None:
            g["top_count"] += 1
            g["top_best"] = rank if g["top_best"] is None else min(g["top_best"], rank)
        g["cats"][d["category_name"] or "—"] += 1
        g["skus"].append({
            "sku": d["sku"], "category_name": d["category_name"],
            "cord_url": d["cord_url"], "vtex_url": d["vtex_url"],
            "cord_value": d["cord_value"], "vtex_value": d["vtex_value"],
            "top_rank": rank,
        })

    rules = []
    for g in groups.values():
        if len(g["skus"]) < min_group:
            continue
        sort_by_impact(g["skus"])
        rules.append({
            "field": g["field"],
            "text": _rule_text(g["field"], g["cord_disc"], g["vtex_disc"]),
            "n": len(g["skus"]),
            "top_count": g["top_count"],
            "top_best": g["top_best"],
            "categories": [f"{name} ({n})" for name, n in g["cats"].most_common(3)],
            "more_cats": max(0, len(g["cats"]) - 3),
            "samples": g["skus"][:6],
        })
    rules.sort(key=lambda r: (-r["top_count"], -r["n"]))
    return rules
