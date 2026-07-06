"""Descubrimiento de productos en CoRD por categoría (crawl de PLP).

CoRD no tiene sitemap (entorno no público) y la paginación de las PLP es client-side
(la 1ª carga server-rendered trae ~32 productos). Para el objetivo de muestrear ≥20
productos por categoría, esa primera carga es suficiente.

Las URLs de las PLP de CoRD se derivan del path de la categoría (slug), validado en
Etapa 0: VTEX `/Tecnologia/Telefonia/Celulares/` -> CoRD `/tecnologia/telefonia/celulares`.
"""
from __future__ import annotations

import json
import re

from .config import Config
from .cord_scraper import _extract_balanced, _flight_text
from .http import HttpClient
from .models import Category, DiscoveredProduct

# enlaces de producto en el payload: "url":"https://.../<slug>-<sku>/p"
_PRODUCT_URL_RE = re.compile(r'"url":"(https?://[^"]+?-(\d{5,})/p)"')
# total de la categoría en CoRD: "totalElements":184 (admite comillas escapadas)
_TOTAL_RE = re.compile(r'totalElements\\?":\s*(\d+)')


def extract_total(html: str) -> "int | None":
    m = _TOTAL_RE.search(html)
    return int(m.group(1)) if m else None


def _parse_sellers(flight: str) -> tuple[dict[str, str], list[str]]:
    """Del array `products[].skus[].seller`: mapa productId->sellerId y lista ordenada
    de sellerIds del SSR (para estimar la proporción Oechsle de la categoría)."""
    by_id: dict[str, str] = {}
    ssr: list[str] = []
    raw = _extract_balanced(flight, '"products":[', "[", "]")
    if not raw:
        return by_id, ssr
    try:
        for p in json.loads(raw):
            pid = str(p.get("productId"))
            for s in (p.get("skus") or []):
                sel = (s.get("seller") or {}).get("sellerId")
                if sel:
                    by_id[pid] = sel
                    ssr.append(sel)
                    break
    except json.JSONDecodeError:
        pass
    return by_id, ssr


def extract_products(
    html: str, category: Category
) -> tuple[list[DiscoveredProduct], list[str]]:
    """Devuelve (productos con URL para validar, lista de sellerIds del SSR para el ratio)."""
    seller_by_id, ssr_sellers = _parse_sellers(_flight_text(html) or html)
    seen: dict[str, DiscoveredProduct] = {}
    for m in _PRODUCT_URL_RE.finditer(html):
        url, sku = m.group(1), m.group(2)
        if sku not in seen:
            seen[sku] = DiscoveredProduct(
                sku=sku, url=url, category_id=category.id, category_name=category.name,
                seller=seller_by_id.get(sku),
            )
    return list(seen.values()), ssr_sellers


class CordDiscovery:
    def __init__(self, cfg: Config, http: HttpClient):
        self.cfg = cfg
        self.http = http
        self.headers = {"User-Agent": cfg.get("cord.user_agent")}

    async def discover_category(
        self, category: Category
    ) -> tuple[list[DiscoveredProduct], "int | None", list[str]]:
        """Descarga la PLP y devuelve (productos con URL, total CoRD, sellerIds del SSR)."""
        html = await self.http.get_text(category.cord_url, headers=self.headers)
        if not html:
            return [], None, []
        products, ssr_sellers = extract_products(html, category)
        return products, extract_total(html), ssr_sellers
