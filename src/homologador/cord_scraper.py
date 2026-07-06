"""Scraper de CoRD (Next.js App Router).

CoRD es server-rendered: los datos del producto vienen embebidos en el payload RSC
(`self.__next_f.push([1,"..."])`) como JSON estructurado. Confirmado en Etapa 0:
- `"pricing":{"regular":{"value":..},"promotional":{"value":..},"offer":{"value":..}}`
- `"specifications":[{"label":..,"value":..}, ...]`  (specs técnicas, ~33)
- `"category":{"id":"217","name":"Celulares"}`, `"brand":{"id","name"}`, `"ean"`, `"permalink"`
- nombre en el `<h1>` del DOM
- breadcrumb de categoría en el bloque JSON-LD (BreadcrumbList)

Parsear el payload es más robusto que el DOM (evita artefactos `<!-- -->`).
"""
from __future__ import annotations

import json
import re
from typing import Optional

from selectolax.parser import HTMLParser

from .config import Config
from .http import HttpClient
from .models import Product

_FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,(".*?")\]\)', re.S)
_CATEGORY_RE = re.compile(r'"category":\{"id":"(\d+)","name":"([^"]*)"')
_BRAND_RE = re.compile(r'"brand":\{"id":"[^"]*","name":"([^"]*)"')
_PERMALINK_RE = re.compile(r'"permalink":"([^"]+)"')
_EAN_RE = re.compile(r'"ean":"([^"]*)"')


def _flight_text(html: str) -> str:
    """Concatena y des-escapa todos los chunks RSC del HTML en un único string JSON."""
    parts = []
    for m in _FLIGHT_RE.finditer(html):
        try:
            parts.append(json.loads(m.group(1)))  # decodifica el literal JS escapado
        except json.JSONDecodeError:
            continue
    return "".join(parts)


def _extract_balanced(text: str, anchor: str, open_ch: str, close_ch: str) -> Optional[str]:
    """Extrae el primer objeto/array JSON balanceado a partir de `anchor`.

    `text` ya es JSON limpio (des-escapado), así que basta con contar llaves
    respetando strings.
    """
    i = text.find(anchor)
    if i < 0:
        return None
    j = text.find(open_ch, i)
    if j < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for k in range(j, len(text)):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return text[j : k + 1]
    return None


def parse_product_html(html: str, sku: str, url: Optional[str] = None) -> Product:
    """Construye un Product a partir del HTML de una página de producto de CoRD."""
    flight = _flight_text(html)
    dom = HTMLParser(html)

    # nombre: h1 del DOM (limpio)
    name = None
    h1 = dom.css_first("h1")
    if h1:
        name = h1.text(strip=True) or None

    # precios: bloque pricing del payload -> tres precios del negocio
    price = list_price = None
    sale_price = promo_price = sip_price = None
    pricing_raw = _extract_balanced(flight, '"pricing":{"sipCredit"', "{", "}")
    if pricing_raw:
        try:
            pricing = json.loads(pricing_raw)
        except json.JSONDecodeError:
            pricing = {}

        def _v(key):
            v = (pricing.get(key) or {}).get("value")
            return float(v) if v else None  # 0 o ausente -> None

        regular = _v("regular")
        promotional = _v("promotional")
        offer = _v("offer")
        # mapeo:
        #  - venta = regular
        #  - con tarjeta SIP = offer si existe; si no, el promocional es el precio con tarjeta
        #  - promocional (sin tarjeta) = promotional SOLO cuando hay offer distinto
        #    (si offer no existe, CoRD muestra directo el precio con tarjeta, no el "sin tarjeta")
        sale_price = regular
        if offer:
            sip_price = offer
            promo_price = promotional
        else:
            sip_price = promotional or regular
            promo_price = None
        # compat
        list_price = regular
        price = sip_price or promo_price or regular

    # especificaciones: array no vacío de {label, value}
    attributes: dict[str, str] = {}
    specs_raw = _extract_balanced(flight, '"specifications":[{"label"', "[", "]")
    if specs_raw:
        try:
            for s in json.loads(specs_raw):
                label = s.get("label")
                value = s.get("value")
                if label and value is not None:
                    attributes[label] = str(value)
        except json.JSONDecodeError:
            pass

    # variantes: array "skus":[{skuId, name, ...}]
    variant_skus: list[str] = []
    skus_raw = _extract_balanced(flight, '"skus":[', "[", "]")
    if skus_raw:
        try:
            for s in json.loads(skus_raw):
                sid = s.get("skuId") or s.get("skuRefId")
                if sid and str(sid) not in variant_skus:
                    variant_skus.append(str(sid))
        except json.JSONDecodeError:
            pass

    # categoría, marca, ean, slug
    cat_id = cat_name = brand = ean = slug = None
    if (m := _CATEGORY_RE.search(flight)):
        cat_id, cat_name = m.group(1), m.group(2)
    if (m := _BRAND_RE.search(flight)):
        brand = m.group(1)
    if (m := _EAN_RE.search(flight)):
        ean = m.group(1)
    if (m := _PERMALINK_RE.search(flight)):
        slug = m.group(1)

    # path de categoría desde el breadcrumb JSON-LD
    category_path = _breadcrumb_path(html)

    # descripción: meta description del producto en el payload
    description = _extract_description(flight)

    return Product(
        sku=str(sku),
        source="cord",
        name=name,
        price=float(price) if price is not None else None,
        list_price=float(list_price) if list_price is not None else None,
        sale_price=sale_price,
        promo_price=promo_price,
        sip_price=sip_price,
        description=description,
        brand=brand,
        category_id=cat_id,
        category_name=cat_name,
        category_path=category_path,
        url=url,
        attributes=attributes,
        variant_skus=variant_skus,
    )


def _breadcrumb_path(html: str) -> Optional[str]:
    """Reconstruye el path de categoría desde el BreadcrumbList JSON-LD."""
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "BreadcrumbList":
            names = [
                el.get("name")
                for el in data.get("itemListElement", [])
                if el.get("name") and el.get("name") != "Inicio"
            ]
            if names:
                return "/" + "/".join(names) + "/"
    return None


def _extract_description(flight: str) -> Optional[str]:
    """Mejor descripción disponible en el payload (meta description del producto)."""
    best = None
    for m in re.finditer(r'"description":"((?:[^"\\]|\\.){20,})"', flight):
        candidate = m.group(1)
        if best is None or len(candidate) > len(best):
            best = candidate
    if best:
        try:
            return json.loads(f'"{best}"')  # des-escapar
        except json.JSONDecodeError:
            return best
    return None


class CordScraper:
    def __init__(self, cfg: Config, http: HttpClient):
        self.cfg = cfg
        self.http = http
        self.base = cfg.get("cord.base_url").rstrip("/")
        self.headers = {"User-Agent": cfg.get("cord.user_agent")}

    async def fetch_product(self, url: str, sku: str) -> Optional[Product]:
        """Descarga y parsea una página de producto de CoRD. None si 404/falla."""
        full = url if url.startswith("http") else f"{self.base}{url}"
        html = await self.http.get_text(full, headers=self.headers)
        if not html:
            return None
        return parse_product_html(html, sku=sku, url=full)
