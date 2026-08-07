"""Cliente de la API pública de catálogo de VTEX (Oechsle).

Etapa 2 del plan: por cada SKU de CoRD, traer el producto VTEX para comparar.
Endpoint confirmado en Etapa 0:
    GET /api/catalog_system/pub/products/search?fq=skuId:<sku>  -> [producto] (1 item)
"""
from __future__ import annotations

import json
from typing import Optional

import re

from .config import Config
from .http import HttpClient
from .models import Category, Product
from .normalize import norm_label, norm_text, slugify_path

# labels de specs visibles en el front de VTEX: <th class="name-field ...">Label</th>
_FRONT_SPEC_RE = re.compile(r'<th class="name-field[^"]*">([^<]+)</th>')


class VtexClient:
    def __init__(self, cfg: Config, http: HttpClient):
        self.cfg = cfg
        self.http = http
        self.base = cfg.get("vtex.base_url").rstrip("/")
        # meta-campos a descartar (normalizados) — no son specs visibles para el cliente
        self.attr_exclude = {
            norm_text(x) for x in (cfg.get("comparators.attributes.exclude") or [])
        }
        # precio con tarjeta SIP/Oh: requiere 1 request extra (simulación de checkout)
        self.fetch_sip = cfg.get("vtex.fetch_sip_price", True)
        self.sip_payment_system = str(cfg.get("vtex.sip_payment_system", "210"))

    async def get_category_tree(self, cord_base: str, depth: int = 50) -> list[Category]:
        """Devuelve las categorías hoja del árbol VTEX, con la PLP de CoRD derivada."""
        url = f"{self.base}/api/catalog_system/pub/category/tree/{depth}"
        text = await self.http.get_text(url)
        if not text:
            return []
        tree = json.loads(text)
        leaves: list[Category] = []

        def walk(node: dict, name_path: list[str], id_path: list[str]) -> None:
            path = name_path + [node["name"]]
            ids = id_path + [str(node["id"])]
            children = node.get("children") or []
            if not children:
                cord_url = cord_base.rstrip("/") + "/" + slugify_path(path)
                leaves.append(
                    Category(
                        id=str(node["id"]),
                        name=node["name"],
                        name_path=path,
                        cord_url=cord_url,
                        vtex_url=node.get("url", ""),
                        id_path=ids,
                    )
                )
            else:
                for c in children:
                    walk(c, path, ids)

        for top in tree:
            walk(top, [], [])
        return leaves

    async def category_count(self, id_path: list[str], seller: str = "1") -> Optional[int]:
        """Total de productos de la categoría en VTEX filtrando por vendedor (Oechsle=1)."""
        if not id_path:
            return None
        path = "/".join(id_path)
        url = (
            f"{self.base}/api/catalog_system/pub/products/search"
            f"?fq=C:/{path}/&fq=sellerId:{seller}&_from=0&_to=0"
        )
        return await self.http.get_count(url)

    async def category_count_spec(self, id_path: list[str], value: str) -> Optional[int]:
        """Total de la categoría filtrando por el spec "Vendido por" (ej. Marketplace)."""
        if not id_path:
            return None
        spec = self.cfg.get("vtex.vendido_por_filter", "specificationFilter_4664")
        path = "/".join(id_path)
        url = (
            f"{self.base}/api/catalog_system/pub/products/search"
            f"?fq=C:/{path}/&fq={spec}:{value}&_from=0&_to=0"
        )
        return await self.http.get_count(url)

    async def get_by_sku(self, sku: str) -> Optional[Product]:
        """Busca un producto por el identificador de la URL (productId), con fallback a skuId.

        El número al final de la URL (CoRD y VTEX) es el **productId**; en productos de una
        sola variante coincide con el skuId, pero en multivariante NO. Por eso se intenta
        productId primero y skuId después.
        """
        for field in ("productId", "skuId"):
            url = f"{self.base}/api/catalog_system/pub/products/search?fq={field}:{sku}"
            text = await self.http.get_text(url)
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if data:
                product = self._parse(data[0], sku)
                if self.fetch_sip:
                    item = self._pick_item(data[0].get("items", []), sku)
                    if item and item.get("itemId"):
                        product.sip_price = await self._sip_price(item["itemId"])
                return product
        return None

    async def get_front_spec_labels(self, url: str) -> Optional[set[str]]:
        """Specs VISIBLES para el cliente en el front de VTEX (labels normalizadas).

        La ficha técnica del front solo renderiza las specs con ShowOnProductPage=true,
        así que sirve de fuente de verdad de "lo que ve el cliente". None si falla.
        """
        if not url:
            return None
        html = await self.http.get_text(url, headers={"User-Agent": "Mozilla/5.0"})
        if not html:
            return None
        return {norm_label(m.group(1).strip()) for m in _FRONT_SPEC_RE.finditer(html)}

    async def get_by_ean(
        self, ean: str, prefer_seller: Optional[str] = None,
        expected_name: Optional[str] = None,
    ) -> Optional[Product]:
        """Busca por código de barras (matching para productos de sellers).

        Un mismo EAN puede estar asignado a VARIOS productos en VTEX (sellers cargan
        EANs erróneos). Si se pasa `expected_name`, se elige el candidato con el nombre
        más parecido y se exige una similitud mínima; si ninguno la alcanza, no hay match.
        """
        if not ean:
            return None
        url = f"{self.base}/api/catalog_system/pub/products/search?fq=alternateIds_Ean:{ean}"
        text = await self.http.get_text(url)
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not data:
            return None

        p = data[0]
        if expected_name and len(data) >= 1:
            from rapidfuzz import fuzz
            from .normalize import norm_text
            threshold = self.cfg.get("matching.name_verify_threshold", 55)
            scored = sorted(
                ((fuzz.token_set_ratio(norm_text(expected_name),
                                       norm_text(c.get("productName") or "")), c)
                 for c in data),
                key=lambda x: -x[0],
            )
            best_score, p = scored[0]
            if best_score < threshold:
                return None  # ningún candidato del EAN se parece: EAN mal asignado

        product = self._parse(p, str(p.get("productId")), prefer_seller=prefer_seller)
        await self._attach_sip(product, p, prefer_seller)
        return product

    async def get_by_slug(self, permalink: str, prefer_seller: Optional[str] = None) -> Optional[Product]:
        """Fallback determinístico: busca por texto y matchea linkText == permalink+'-<id>'.

        El linkText de VTEX es exactamente el permalink de CoRD + '-' + productId
        (misma fuente de slugs), así que la igualdad de slug-sin-id identifica el producto.
        """
        if not permalink:
            return None
        words = permalink.replace("-", " ")
        url = (f"{self.base}/api/catalog_system/pub/products/search"
               f"?ft={words}&_from=0&_to=15")
        text = await self.http.get_text(url)
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        for p in data or []:
            link = p.get("linkText") or ""
            base = re.sub(r"-\d+$", "", link)
            if base == permalink:
                product = self._parse(p, str(p.get("productId")), prefer_seller=prefer_seller)
                await self._attach_sip(product, p, prefer_seller)
                return product
        return None

    async def _attach_sip(self, product: Product, p: dict, prefer_seller: Optional[str]) -> None:
        if self.fetch_sip:
            item = self._pick_item(p.get("items", []), product.sku)
            if item and item.get("itemId"):
                product.sip_price = await self._sip_price(
                    item["itemId"], seller=prefer_seller or "1"
                )

    async def _sip_price(self, item_id: str, seller: str = "1") -> Optional[float]:
        """Precio con tarjeta SIP/Oh vía simulación de checkout con el medio de pago."""
        url = f"{self.base}/api/checkout/pub/orderForms/simulation?sc=1"
        payload = {
            "items": [{"id": str(item_id), "quantity": 1, "seller": str(seller)}],
            "country": "PER",
            "paymentData": {"payments": [{
                "paymentSystem": self.sip_payment_system,
                "referenceValue": 100000, "installments": 1, "value": 100000,
            }]},
        }
        text = await self.http.post_json(url, payload)
        if not text:
            return None
        try:
            items = json.loads(text).get("items") or []
        except json.JSONDecodeError:
            return None
        if items and items[0].get("sellingPrice"):
            return round(items[0]["sellingPrice"] / 100.0, 2)
        return None

    @staticmethod
    def _pick_item(items: list[dict], sku: str) -> Optional[dict]:
        """Elige la variante a comparar: la que matchea el id, luego una disponible, luego la 1ª."""
        if not items:
            return None
        for it in items:  # match exacto de itemId (productos de una sola variante)
            if str(it.get("itemId")) == str(sku):
                return it
        for it in items:  # primera con stock disponible
            offer = (it.get("sellers") or [{}])[0].get("commertialOffer", {})
            if (offer.get("AvailableQuantity", 0) or 0) > 0:
                return it
        return items[0]

    @staticmethod
    def _pick_seller(item: dict, prefer_seller: Optional[str]) -> dict:
        """Oferta del seller preferido (mismo vendedor que en CoRD); si no está, la primera."""
        sellers = item.get("sellers") or [{}]
        if prefer_seller:
            for s in sellers:
                if str(s.get("sellerId", "")).lower() == str(prefer_seller).lower():
                    return s
        return sellers[0]

    def _parse(self, p: dict, sku: str, prefer_seller: Optional[str] = None) -> Product:
        item = self._pick_item(p.get("items", []), sku)
        price = list_price = None
        available = None
        if item:
            offer = self._pick_seller(item, prefer_seller).get("commertialOffer", {})
            price = offer.get("Price")
            list_price = offer.get("ListPrice")
            available = (offer.get("AvailableQuantity", 0) or 0) > 0

        # atributos visibles para el cliente: solo los listados en `allSpecifications`
        # de VTEX, descartando los meta-campos configurados.
        attributes: dict[str, str] = {}
        for key in p.get("allSpecifications", []) or []:
            if norm_text(key) in self.attr_exclude:
                continue
            val = p.get(key)
            if isinstance(val, list) and val and all(isinstance(x, str) for x in val):
                value = ", ".join(val).strip()
                if value:  # ignorar specs sin valor (no se muestran al cliente)
                    attributes[key] = value

        categories = p.get("categories") or []
        cat_path = categories[0] if categories else None
        cat_name = cat_path.strip("/").split("/")[-1] if cat_path else None

        desc = None
        if isinstance(p.get("Descripción Web"), list) and p["Descripción Web"]:
            desc = p["Descripción Web"][0]
        if not desc:
            desc = p.get("description") or p.get("metaTagDescription")

        variant_skus = [str(it.get("itemId")) for it in p.get("items", []) if it.get("itemId")]

        return Product(
            sku=str(sku),
            source="vtex",
            name=p.get("productName"),
            price=float(price) if price is not None else None,
            list_price=float(list_price) if list_price is not None else None,
            description=desc,
            brand=p.get("brand"),
            category_path=cat_path,
            category_name=cat_name,
            url=p.get("link"),
            available=available,
            attributes=attributes,
            variant_skus=variant_skus,
            sale_price=float(list_price) if list_price is not None else None,
            promo_price=float(price) if price is not None else None,
            # sip_price se completa aparte vía simulación en get_by_sku
        )
