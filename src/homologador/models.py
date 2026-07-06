"""Modelo común de datos para productos de ambos sistemas y resultados de comparación.

Tanto el scraper de CoRD como el cliente de VTEX normalizan a `Product`, de modo que
los comparadores trabajan siempre contra la misma estructura sin importar el origen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


@dataclass
class Product:
    """Producto normalizado, común a CoRD y VTEX."""

    sku: str
    source: str                       # "cord" | "vtex"
    name: Optional[str] = None
    price: Optional[float] = None     # precio vigente (compat)
    list_price: Optional[float] = None  # precio lista / tachado (compat)
    # los tres precios del negocio:
    sale_price: Optional[float] = None   # precio de venta (lista) — CoRD regular / VTEX ListPrice
    promo_price: Optional[float] = None  # precio promocional sin tarjeta — CoRD promotional / VTEX Price
    sip_price: Optional[float] = None    # precio con tarjeta SIP/Oh — CoRD offer / VTEX simulación
    description: Optional[str] = None
    brand: Optional[str] = None
    category_id: Optional[str] = None
    category_name: Optional[str] = None
    category_path: Optional[str] = None  # ej. "/Tecnologia/Telefonia/Celulares/"
    url: Optional[str] = None
    available: Optional[bool] = None
    # atributos/especificaciones normalizados: {label -> value}
    attributes: dict[str, str] = field(default_factory=dict)
    # SKUs de las variantes del producto (colores/tallas/etc.)
    variant_skus: list[str] = field(default_factory=list)

    @property
    def exists(self) -> bool:
        return self.name is not None or self.price is not None


@dataclass
class Category:
    """Categoría hoja del árbol, con su path de nombres e IDs."""

    id: str
    name: str
    name_path: list[str]          # ej. ["Tecnologia", "Telefonia", "Celulares"]
    cord_url: str                 # PLP de CoRD derivada del slug del path
    vtex_url: str = ""            # PLP de VTEX (campo `url` del árbol de categorías)
    id_path: list[str] = field(default_factory=list)  # ej. ["160","170","217"] para fq=C:

    @property
    def path_str(self) -> str:
        return " > ".join(self.name_path)


@dataclass
class DiscoveredProduct:
    """Producto descubierto en una PLP de CoRD (universo a validar)."""

    sku: str
    url: str
    category_id: str
    category_name: str
    seller: Optional[str] = None   # sellerId de CoRD (ej. "oechsle", "plazavea")


class Severity(str, Enum):
    """Tipo/severidad de discrepancia que reporta un comparador."""

    OK = "OK"
    FALTANTE = "FALTANTE"          # producto/dato ausente en uno de los lados
    PRECIO = "PRECIO"
    NOMBRE = "NOMBRE"
    DESCRIPCION = "DESCRIPCION"
    ATRIBUTO = "ATRIBUTO"
    VARIANTE = "VARIANTE"          # faltan variantes (colores/tallas) en CoRD
    VARIANTE_EXTRA = "VARIANTE_EXTRA"  # CoRD tiene variantes que ya no están en VTEX


@dataclass
class FieldResult:
    """Resultado de un comparador para un campo de un producto."""

    field: str                 # clave del comparador, ej. "price"
    ok: bool
    score: float               # 0.0 - 1.0
    severity: Severity
    detail: str = ""           # explicación legible (cord vs vtex)
    cord_value: Optional[str] = None
    vtex_value: Optional[str] = None
    extra: dict = field(default_factory=dict)  # datos estructurados (ej. labels faltantes)


@dataclass
class ProductComparison:
    """Comparación completa de un producto (CoRD vs VTEX) con todos los campos."""

    sku: str
    category_name: Optional[str]
    cord_url: Optional[str]
    vtex_url: Optional[str]
    vtex_found: bool
    fields: list[FieldResult] = field(default_factory=list)
    score: float = 0.0         # score de homologación 0-100 (ponderado)
    error: Optional[str] = None
