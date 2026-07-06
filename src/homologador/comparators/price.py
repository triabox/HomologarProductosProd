"""Comparadores de los tres precios del negocio: venta, promocional y con tarjeta SIP.

Mapeo CoRD ↔ VTEX (verificado con datos):
- Venta:        CoRD regular      ↔ VTEX ListPrice
- Promocional:  CoRD promotional  ↔ VTEX Price (catálogo)   [solo si CoRD expone offer distinto]
- SIP (tarjeta):CoRD offer/promo  ↔ VTEX simulación con Tarjeta Oh (paymentSystem 210)
"""
from __future__ import annotations

from ..models import FieldResult, Product, Severity
from ..normalize import norm_price
from .base import Comparator, register


def _compare(
    field: str, cord_val, vtex_val, tolerance: float, na_if_missing: bool
) -> FieldResult:
    c, v = norm_price(cord_val), norm_price(vtex_val)
    if c is None and v is None:
        return FieldResult(field, ok=True, score=1.0, severity=Severity.OK,
                           detail="sin este precio en ninguno de los dos")
    if c is None or v is None:
        if na_if_missing:
            return FieldResult(field, ok=True, score=1.0, severity=Severity.OK,
                               detail="no aplica (un sistema no expone este precio)",
                               cord_value=str(c), vtex_value=str(v))
        return FieldResult(field, ok=False, score=0.0, severity=Severity.FALTANTE,
                           detail="precio ausente en un sistema",
                           cord_value=str(c), vtex_value=str(v))
    if v == 0:
        diff = 0.0 if c == 0 else 100.0
    else:
        diff = abs(c - v) / v * 100.0
    ok = diff <= tolerance
    return FieldResult(
        field=field, ok=ok, score=1.0 if ok else max(0.0, 1.0 - diff / 100.0),
        severity=Severity.OK if ok else Severity.PRECIO,
        detail=f"desvío {diff:.2f}% (tol {tolerance}%)",
        cord_value=f"{c:.2f}", vtex_value=f"{v:.2f}",
    )


@register
class PrecioVentaComparator(Comparator):
    key = "precio_venta"
    label = "Precio venta"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.tol = cfg.get("comparators.precio_venta.tolerance_pct", 0.0)

    def compare(self, cord: Product, vtex: Product) -> FieldResult:
        # el precio de venta debería existir y coincidir siempre; ausencia = error
        return _compare(self.key, cord.sale_price, vtex.sale_price, self.tol,
                        na_if_missing=False)


@register
class PrecioPromocionalComparator(Comparator):
    key = "precio_promocional"
    label = "Precio promo"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.tol = cfg.get("comparators.precio_promocional.tolerance_pct", 1.0)

    def compare(self, cord: Product, vtex: Product) -> FieldResult:
        # solo cuando CoRD expone un precio sin-tarjeta distinto (offer presente)
        return _compare(self.key, cord.promo_price, vtex.promo_price, self.tol,
                        na_if_missing=True)


@register
class PrecioSipComparator(Comparator):
    key = "precio_sip"
    label = "Precio SIP"

    def __init__(self, cfg):
        super().__init__(cfg)
        self.tol = cfg.get("comparators.precio_sip.tolerance_pct", 2.0)

    def compare(self, cord: Product, vtex: Product) -> FieldResult:
        # precio final con tarjeta: CoRD offer, o promotional si no hay offer distinto
        cord_sip = cord.sip_price if cord.sip_price is not None else cord.promo_price
        return _compare(self.key, cord_sip, vtex.sip_price, self.tol,
                        na_if_missing=True)
