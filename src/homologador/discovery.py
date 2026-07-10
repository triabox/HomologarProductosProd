"""Descubrimiento de productos por categoría vía la API REST de CoRD.

Históricamente esto scrapeaba las PLPs server-rendered (limitadas a ~32 productos).
Desde jul-2026 CoRD es client-side y expone /search/v3/products, que da paginación
real y el total exacto de la categoría (totalElements).

Los categoryIds de CoRD coinciden con los del árbol de VTEX (ej. 217 = Celulares).
"""
from __future__ import annotations

from .config import Config
from .cord_api import CordApi
from .http import HttpClient
from .models import Category, DiscoveredProduct


class CordDiscovery:
    def __init__(self, cfg: Config, http: HttpClient):
        self.api = CordApi(cfg, http)

    async def discover_category(
        self, category: Category, size: int = 50
    ) -> tuple[list[DiscoveredProduct], "int | None", list[str]]:
        """Devuelve (productos descubiertos, total CoRD de la categoría, sellers vistos)."""
        items, total = await self.api.list_category(category.id, page=0, size=size)
        products: list[DiscoveredProduct] = []
        sellers: list[str] = []
        for item in items:
            dp = self.api.item_to_discovered(item, category)
            if dp is None:
                continue
            products.append(dp)
            if dp.seller:
                sellers.append(dp.seller)
        return products, total, sellers
