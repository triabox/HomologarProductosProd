"""Comparador de atributos: cobertura (presentes en ambos) + coincidencia de valor.

Score = coverage_weight * cobertura + value_weight * coincidencia_de_valor.
- cobertura: |labels en ambos| / |labels en VTEX|  (VTEX es la fuente de verdad)
- coincidencia: de los labels comunes, % con valor normalizado igual (o casi, fuzzy)
"""
from __future__ import annotations

from rapidfuzz import fuzz

from ..models import FieldResult, Product, Severity
from ..normalize import norm_attributes, norm_label
from .base import Comparator, register

_VALUE_FUZZY_OK = 92  # umbral fuzzy para considerar dos valores equivalentes


@register
class AttributesComparator(Comparator):
    key = "attributes"
    label = "Atributos"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.cov_w = cfg.get("comparators.attributes.coverage_weight", 0.4)
        self.val_w = cfg.get("comparators.attributes.value_weight", 0.6)
        # labels aprendidas como NO visibles en el front de VTEX (se inyectan en runtime)
        self.learned_exclude: set[str] = set()

    def compare(self, cord: Product, vtex: Product) -> FieldResult:
        c = norm_attributes(cord.attributes)
        v = norm_attributes(vtex.attributes)
        # descartar labels aprendidas como no visibles para el cliente
        if self.learned_exclude:
            c = {k: val for k, val in c.items() if k not in self.learned_exclude}
            v = {k: val for k, val in v.items() if k not in self.learned_exclude}
        # mapa label-normalizada -> label original de VTEX, para mostrar nombres legibles
        v_orig = {norm_label(k): k for k in vtex.attributes}
        if not v:
            return FieldResult(
                field=self.key, ok=False, score=0.0, severity=Severity.FALTANTE,
                detail="VTEX no tiene atributos para comparar",
                cord_value=str(len(c)), vtex_value="0",
            )

        common = set(c) & set(v)
        coverage = len(common) / len(v)

        matches = 0
        mismatches: list[str] = []
        for label in common:
            if c[label] == v[label] or fuzz.ratio(c[label], v[label]) >= _VALUE_FUZZY_OK:
                matches += 1
            else:
                mismatches.append(label)
        value_match = (matches / len(common)) if common else 0.0

        score = self.cov_w * coverage + self.val_w * value_match
        # OK si cobertura completa y todos los valores comunes coinciden
        ok = coverage >= 0.99 and value_match >= 0.99
        # labels con nombre original de VTEX (legible)
        missing = sorted(v_orig.get(k, k) for k in (set(v) - set(c)))
        mismatch_labels = sorted(v_orig.get(k, k) for k in mismatches)
        detail = (
            f"cobertura {coverage*100:.0f}% ({len(common)}/{len(v)}), "
            f"valores OK {value_match*100:.0f}%"
        )
        if missing:
            detail += f" | faltan en CoRD: {', '.join(missing[:5])}"
        if mismatch_labels:
            detail += f" | difieren: {', '.join(mismatch_labels[:5])}"
        return FieldResult(
            field=self.key,
            ok=ok,
            score=round(score, 4),
            severity=Severity.OK if ok else Severity.ATRIBUTO,
            detail=detail,
            cord_value=str(len(c)),
            vtex_value=str(len(v)),
            extra={"missing": missing, "mismatch": mismatch_labels},
        )
