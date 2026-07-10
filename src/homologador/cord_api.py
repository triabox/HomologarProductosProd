"""Cliente de la API REST de CoRD (api.cord.pe/cord-rest).

Desde jul-2026 el frontend de CoRD es client-side: el HTML ya no trae datos y todo
sale de esta API (descubierta en los bundles JS del sitio). Ventajas sobre el
scraping anterior:
- precios CON TIPO explícito: REGULAR / PROMOTIONAL_GENERAL / PROMOTIONAL_SIP_CREDITO
- paginación real por categoría (sin el límite de ~32 del SSR) + totalElements exacto
- seller explícito por SKU

Auth: token anónimo vía POST /iam/v1/auth/anonymous (JWT, se renueva ante 401).
"""
from __future__ import annotations

import json
from typing import Optional

from .config import Config
from .http import HttpClient
from .models import Category, DiscoveredProduct, Product

# tipos de precio de la API -> campos del modelo
_PRICE_MAP = {
    "REGULAR": "sale_price",
    "PROMOTIONAL_GENERAL": "promo_price",
    "PROMOTIONAL_SIP_CREDITO": "sip_price",
}


class CordApi:
    def __init__(self, cfg: Config, http: HttpClient):
        self.cfg = cfg
        self.http = http
        self.base = cfg.get("cord.api_base").rstrip("/")
        self.site = cfg.get("cord.base_url").rstrip("/")
        self._headers_base = {
            "User-Agent": cfg.get("cord.user_agent"),
            "X-Platform": "WEB",
            "X-Application": cfg.get("cord.application", "STOREFRONT"),
            "X-Store-Id": cfg.get("cord.store_id", ""),
            "Origin": self.site,
        }
        self._token: Optional[str] = None

    # -- auth ---------------------------------------------------------------
    async def _get_token(self, force: bool = False) -> Optional[str]:
        if self._token and not force:
            return self._token
        text = await self.http.post_json(
            f"{self.base}/iam/v1/auth/anonymous", {},
            headers=self._headers_base, use_cache=False,
        )
        if not text:
            return None
        try:
            self._token = json.loads(text).get("idToken")
        except json.JSONDecodeError:
            self._token = None
        return self._token

    async def _get(self, path: str, extra_headers: Optional[dict] = None) -> Optional[dict]:
        """GET autenticado; renueva el token anónimo una vez si expira."""
        for attempt in (1, 2):
            tok = await self._get_token(force=(attempt == 2))
            if not tok:
                return None
            headers = {**self._headers_base, "Authorization": f"Bearer {tok}",
                       **(extra_headers or {})}
            text = await self.http.get_text(f"{self.base}{path}", headers=headers)
            if not text:
                if attempt == 1:
                    continue  # posible 401 por token vencido: reintenta con token nuevo
                return None
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return None
            if isinstance(data, dict) and data.get("error", {}).get("code") == "AUTHENTICATION_ERROR":
                if attempt == 1:
                    continue
                return None
            return data
        return None

    # -- catálogo -----------------------------------------------------------
    async def list_category(
        self, category_id: str, page: int = 0, size: int = 50
    ) -> tuple[list[dict], Optional[int]]:
        """Items de una categoría + total exacto de productos (totalElements)."""
        data = await self._get(
            f"/search/v3/products?categoryIds={category_id}&page={page}&size={size}"
        )
        if not data:
            return [], None
        items = data.get("items") or []
        total = (data.get("page") or {}).get("totalElements")
        return items, total

    async def get_product(self, permalink: str, sku: str) -> Optional[Product]:
        data = await self._get(
            f"/search/v3/products/p/{permalink}",
            extra_headers={"X-Multivalued-Specs": "true"},
        )
        if not data or not isinstance(data, dict) or not data.get("name"):
            return None
        return self._to_product(data, sku)

    # -- mapeo --------------------------------------------------------------
    @staticmethod
    def _sku_entry(p: dict) -> dict:
        return (p.get("skus") or [{}])[0]

    def _to_product(self, p: dict, sku: str) -> Product:
        sku0 = self._sku_entry(p)
        seller = sku0.get("seller") or {}

        prices = {"sale_price": None, "promo_price": None, "sip_price": None}
        for pr in seller.get("prices") or []:
            field = _PRICE_MAP.get(pr.get("type"))
            if field and pr.get("value"):
                prices[field] = float(pr["value"])

        attributes: dict[str, str] = {}
        for spec in p.get("specifications") or []:
            name = spec.get("name")
            values = [v.get("value") for v in (spec.get("values") or []) if v.get("value")]
            if name and values:
                attributes[name] = ", ".join(str(v) for v in values)

        cat = p.get("category") or {}
        path_names = [x.get("name") for x in (cat.get("path") or []) if x.get("name")]
        if cat.get("name") and cat["name"] not in path_names:
            path_names.append(cat["name"])

        permalink = (p.get("seo") or {}).get("permalink") or ""
        available = (seller.get("availableUnits") or 0) > 0

        return Product(
            sku=str(sku),
            source="cord",
            name=p.get("name"),
            price=prices["sip_price"] or prices["promo_price"] or prices["sale_price"],
            list_price=prices["sale_price"],
            sale_price=prices["sale_price"],
            promo_price=prices["promo_price"],
            sip_price=prices["sip_price"],
            description=p.get("description") or (p.get("seo") or {}).get("metaDescription"),
            brand=(p.get("brand") or {}).get("name"),
            category_id=str(cat.get("id")) if cat.get("id") else None,
            category_name=cat.get("name"),
            category_path="/" + "/".join(path_names) + "/" if path_names else None,
            url=f"{self.site}/{permalink}/p" if permalink else None,
            available=available,
            attributes=attributes,
            variant_skus=[str(s.get("skuId")) for s in (p.get("skus") or []) if s.get("skuId")],
        )

    def item_to_discovered(self, item: dict, category: Category) -> Optional[DiscoveredProduct]:
        """Convierte un item del listado en DiscoveredProduct (sku = productId)."""
        pid = item.get("productId")
        permalink = (item.get("seo") or {}).get("permalink")
        if not pid or not permalink:
            return None
        seller = (self._sku_entry(item).get("seller") or {}).get("sellerId")
        return DiscoveredProduct(
            sku=str(pid),
            url=f"{self.site}/{permalink}/p",
            category_id=category.id,
            category_name=category.name,
            seller=seller,
        )
