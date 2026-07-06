"""Comparador de descripción: similitud de texto (rapidfuzz)."""
from __future__ import annotations

from rapidfuzz import fuzz

from ..models import FieldResult, Product, Severity
from ..normalize import norm_text
from .base import Comparator, register


@register
class DescriptionComparator(Comparator):
    key = "description"
    label = "Descripción"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.threshold = cfg.get("comparators.description.fuzzy_threshold", 80)

    def compare(self, cord: Product, vtex: Product) -> FieldResult:
        c, v = norm_text(cord.description), norm_text(vtex.description)
        if not c or not v:
            return FieldResult(
                field=self.key, ok=False, score=0.0, severity=Severity.FALTANTE,
                detail="descripción ausente en uno de los sistemas",
                cord_value=(cord.description or "")[:80],
                vtex_value=(vtex.description or "")[:80],
            )
        ratio = fuzz.token_set_ratio(c, v)
        ok = ratio >= self.threshold
        return FieldResult(
            field=self.key,
            ok=ok,
            score=ratio / 100.0,
            severity=Severity.OK if ok else Severity.DESCRIPCION,
            detail=f"similitud {ratio:.0f}% (umbral {self.threshold}%)",
            cord_value=(cord.description or "")[:120],
            vtex_value=(vtex.description or "")[:120],
        )
