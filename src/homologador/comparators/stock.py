"""Comparador de disponibilidad (¿tiene stock?) entre CoRD y VTEX.

Compara solo el booleano "hay stock / no hay stock" — no las unidades exactas:
el inventario cambia constantemente y comparar cantidades exactas sería puro
ruido de timing, no una discrepancia real. Ambos lados ya traen este dato en
el mismo request usado para precio/nombre/atributos (sin costo extra).

Dos direcciones de fallo, ambas relevantes:
- CoRD SIN stock y VTEX CON stock -> venta perdida (cliente no puede comprar en CoRD).
- CoRD CON stock y VTEX SIN stock -> riesgo de sobreventa (CoRD promete algo que
  el sistema de origen ya no tiene).
"""
from __future__ import annotations

from ..models import FieldResult, Product, Severity
from .base import Comparator, register


@register
class StockComparator(Comparator):
    key = "stock"
    label = "Stock"

    def compare(self, cord: Product, vtex: Product) -> FieldResult:
        c, v = cord.available, vtex.available
        if c is None or v is None:
            return FieldResult(
                self.key, ok=True, score=1.0, severity=Severity.NO_APLICA,
                detail="disponibilidad no informada por uno de los dos sistemas",
            )
        if c == v:
            estado = "con stock" if c else "sin stock"
            return FieldResult(
                self.key, ok=True, score=1.0, severity=Severity.OK,
                detail=f"coincide ({estado})", cord_value=str(c), vtex_value=str(v),
            )
        detail = (
            "CoRD sin stock pero VTEX vende (venta perdida)" if v and not c
            else "CoRD vende pero VTEX no tiene stock (riesgo de sobreventa)"
        )
        return FieldResult(
            self.key, ok=False, score=0.0, severity=Severity.STOCK,
            detail=detail, cord_value=str(c), vtex_value=str(v),
        )
