"""Cálculo de KPIs por categoría y globales, y deltas vs la corrida anterior."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import ProductComparison
from .storage import Storage


@dataclass
class CategoryAgg:
    category_name: str
    sampled: int
    vtex_found: int
    avg_score: float
    field_ok: dict[str, float]   # field -> % de productos OK (sobre los vtex_found)


def aggregate_category(name: str, comps: list[ProductComparison]) -> CategoryAgg:
    """Agrega los resultados de una categoría a partir de las comparaciones."""
    sampled = len(comps)
    found = [c for c in comps if c.vtex_found]
    vtex_found = len(found)
    avg_score = round(sum(c.score for c in found) / vtex_found, 2) if vtex_found else 0.0

    field_ok: dict[str, float] = {}
    if found:
        keys = {fr.field for c in found for fr in c.fields}
        for k in keys:
            oks = sum(1 for c in found for fr in c.fields if fr.field == k and fr.ok)
            field_ok[k] = round(oks / vtex_found * 100.0, 1)
    return CategoryAgg(name, sampled, vtex_found, avg_score, field_ok)


@dataclass
class GlobalSummary:
    products_compared: int
    vtex_found: int
    coverage_pct: float            # % de SKUs de CoRD hallados en VTEX
    avg_score: float
    field_ok: dict[str, float]     # % OK por campo
    severity_counts: dict[str, int]
    categories_done: int


def global_summary(storage: Storage, run_id: int) -> GlobalSummary:
    prods = storage.product_results(run_id)
    fields = storage.field_results(run_id)
    cats = storage.category_stats(run_id)

    compared = len(prods)
    found = [p for p in prods if p["vtex_found"]]
    n_found = len(found)
    coverage = round(n_found / compared * 100.0, 1) if compared else 0.0
    avg_score = round(sum(p["score"] for p in found) / n_found, 2) if n_found else 0.0

    field_ok: dict[str, float] = {}
    severity_counts: dict[str, int] = {}
    by_field: dict[str, list[int]] = {}
    for fr in fields:
        by_field.setdefault(fr["field"], []).append(fr["ok"])
        sev = fr["severity"]
        if sev != "OK":
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
    for k, oks in by_field.items():
        field_ok[k] = round(sum(oks) / len(oks) * 100.0, 1) if oks else 0.0

    return GlobalSummary(
        products_compared=compared,
        vtex_found=n_found,
        coverage_pct=coverage,
        avg_score=avg_score,
        field_ok=field_ok,
        severity_counts=severity_counts,
        categories_done=len(cats),
    )


@dataclass
class Deltas:
    avg_score: Optional[float] = None
    coverage_pct: Optional[float] = None
    field_ok: dict[str, float] = field(default_factory=dict)


def compute_deltas(current: GlobalSummary, previous: Optional[GlobalSummary]) -> Deltas:
    if previous is None:
        return Deltas()
    d = Deltas(
        avg_score=round(current.avg_score - previous.avg_score, 2),
        coverage_pct=round(current.coverage_pct - previous.coverage_pct, 1),
    )
    for k, v in current.field_ok.items():
        if k in previous.field_ok:
            d.field_ok[k] = round(v - previous.field_ok[k], 1)
    return d
