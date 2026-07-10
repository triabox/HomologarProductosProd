"""Obtención del detalle de producto de CoRD.

Históricamente parseaba el HTML server-rendered (payload RSC de Next.js). Desde
jul-2026 el front de CoRD es client-side y el detalle sale de la API REST
(`/search/v3/products/p/<permalink>`), con precios tipados y specs estructuradas.

Se mantiene la clase CordScraper con la misma interfaz (`fetch_product(url, sku)`)
para no tocar el resto del pipeline.
"""
from __future__ import annotations

import re
from typing import Optional

from .config import Config
from .cord_api import CordApi
from .http import HttpClient
from .models import Product

# permalink dentro de la URL de producto: https://oechsle.cord.pe/<permalink>/p
_PERMALINK_RE = re.compile(r"^https?://[^/]+/(.+?)/p/?$")


def permalink_from_url(url: str) -> Optional[str]:
    m = _PERMALINK_RE.match(url or "")
    return m.group(1) if m else None


class CordScraper:
    def __init__(self, cfg: Config, http: HttpClient):
        self.api = CordApi(cfg, http)

    async def fetch_product(self, url: str, sku: str) -> Optional[Product]:
        """Trae el producto por su URL de CoRD (usa el permalink contra la API)."""
        permalink = permalink_from_url(url) or url.strip("/")
        if not permalink:
            return None
        return await self.api.get_product(permalink, sku=sku)
