"""Comparador de variantes: ¿CoRD tiene todas las variantes (SKUs) que tiene VTEX?

VTEX expone las variantes en `items[].itemId`; CoRD en el array `skus[].skuId`. Ambos
usan el mismo número de SKU, así que se comparan por conjunto.
"""
from __future__ import annotations

from ..models import FieldResult, Product, Severity
from .base import Comparator, register


@register
class VariantsComparator(Comparator):
    key = "variants"
    label = "Variantes"

    def compare(self, cord: Product, vtex: Product) -> FieldResult:
        vtex_set = {str(s) for s in vtex.variant_skus}
        cord_set = {str(s) for s in cord.variant_skus}
        # si no se pudo leer la lista de CoRD, usar el SKU del propio producto
        if not cord_set and cord.sku:
            cord_set = {str(cord.sku)}

        if not vtex_set:
            return FieldResult(
                field=self.key, ok=True, score=1.0, severity=Severity.OK,
                detail="VTEX sin variantes para comparar",
                cord_value=str(len(cord_set)), vtex_value="0",
            )

        union = vtex_set | cord_set
        covered = len(vtex_set & cord_set)
        missing = sorted(vtex_set - cord_set)
        extra = sorted(cord_set - vtex_set)
        # score por Jaccard: penaliza tanto faltantes como sobrantes
        score = covered / len(union) if union else 1.0
        ok = not missing and not extra

        # severidad: faltantes tiene prioridad; si solo sobran, marcar como EXTRA
        if ok:
            severity = Severity.OK
        elif missing:
            severity = Severity.VARIANTE
        else:
            severity = Severity.VARIANTE_EXTRA

        detail = f"CoRD {len(cord_set)} / VTEX {len(vtex_set)} variantes"
        if missing:
            detail += f" | faltan en CoRD: {', '.join(missing[:8])}"
        if extra:
            detail += f" | extra en CoRD (revisar): {', '.join(extra[:8])}"

        return FieldResult(
            field=self.key,
            ok=ok,
            score=round(score, 4),
            severity=severity,
            detail=detail,
            cord_value=str(len(cord_set)),
            vtex_value=str(len(vtex_set)),
            extra={"missing": missing, "extra": extra},
        )
