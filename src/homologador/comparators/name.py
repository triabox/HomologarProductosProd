"""Comparador de nombre: exacto + fuzzy (rapidfuzz token_set_ratio)."""
from __future__ import annotations

from rapidfuzz import fuzz

from ..models import FieldResult, Product, Severity
from ..normalize import norm_text
from .base import Comparator, register


@register
class NameComparator(Comparator):
    key = "name"
    label = "Nombre"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.threshold = cfg.get("comparators.name.fuzzy_threshold", 90)

    def compare(self, cord: Product, vtex: Product) -> FieldResult:
        c, v = norm_text(cord.name), norm_text(vtex.name)
        if not c or not v:
            return FieldResult(
                field=self.key, ok=False, score=0.0, severity=Severity.FALTANTE,
                detail="nombre ausente", cord_value=cord.name, vtex_value=vtex.name,
            )
        ratio = fuzz.token_set_ratio(c, v)
        ok = ratio >= self.threshold
        return FieldResult(
            field=self.key,
            ok=ok,
            score=ratio / 100.0,
            severity=Severity.OK if ok else Severity.NOMBRE,
            detail=f"similitud {ratio:.0f}% (umbral {self.threshold}%)",
            cord_value=cord.name,
            vtex_value=vtex.name,
        )
