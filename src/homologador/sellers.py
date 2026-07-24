"""Auditoría de sellers: qué vendedores existen en CoRD y cómo van contra VTEX.

Corrida SEPARADA de la validación de productos Oechsle (`homologador sellers`):
1. Barre TODO el catálogo de CoRD por categoría (API, paginado) y agrega por seller:
   productos únicos y categorías donde aparece.
2. Para cada seller, consulta el total de productos en VTEX (`fq=sellerId:`;
   oechsle en VTEX es el seller "1").
3. Persiste un snapshot histórico y calcula el % de migración por seller.
"""
from __future__ import annotations

import asyncio
import collections
from datetime import datetime, timezone

from .config import Config
from .cord_api import CordApi
from .http import HttpClient
from .storage import Storage
from .vtex_client import VtexClient

# sellerId de CoRD -> sellerId de VTEX cuando difieren
_VTEX_SELLER_MAP = {"oechsle": "1"}


class SellersAudit:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    async def run(self) -> int:
        storage = Storage(self.cfg.path("paths.db"))
        async with HttpClient(self.cfg) as http:
            api = CordApi(self.cfg, http)
            vtex = VtexClient(self.cfg, http)

            cats = await vtex.get_category_tree(self.cfg.get("cord.base_url"))
            print(f"[sellers] barriendo {len(cats)} categorías de CoRD...")

            counts: collections.Counter = collections.Counter()
            names: dict[str, str] = {}
            seller_cats: dict[str, set] = collections.defaultdict(set)
            seen: set = set()

            async def sweep(c):
                page = 0
                while True:
                    data = await api._get(
                        f"/search/v3/products?categoryIds={c.id}&page={page}"
                        f"&size=100&includeOutOfStock=true"
                    )
                    if not data:
                        return
                    items = data.get("items") or []
                    for it in items:
                        pid = it.get("productId")
                        if pid in seen:
                            continue
                        seen.add(pid)
                        seller = (it.get("skus") or [{}])[0].get("seller") or {}
                        sid = seller.get("sellerId")
                        if sid:
                            counts[sid] += 1
                            names.setdefault(sid, seller.get("sellerName") or sid)
                            seller_cats[sid].add(c.name)
                    total = (data.get("page") or {}).get("totalElements") or 0
                    page += 1
                    if page * 100 >= min(total, 10000) or not items:
                        return

            CHUNK = 24
            for i in range(0, len(cats), CHUNK):
                await asyncio.gather(*(sweep(c) for c in cats[i:i + CHUNK]))
                if (i // CHUNK) % 10 == 0:
                    print(f"[sellers]   {min(i+CHUNK,len(cats))}/{len(cats)} categorías")

            print(f"[sellers] CoRD: {len(seen)} productos, {len(counts)} sellers. "
                  f"Consultando VTEX por seller...")

            vtex_totals: dict[str, int] = {}

            async def vcount(sid: str):
                vid = _VTEX_SELLER_MAP.get(sid, sid)
                url = (f"{vtex.base}/api/catalog_system/pub/products/search"
                       f"?fq=sellerId:{vid}&_from=0&_to=0")
                vtex_totals[sid] = await http.get_count(url)

            sids = list(counts)
            for i in range(0, len(sids), 16):
                await asyncio.gather(*(vcount(s) for s in sids[i:i + 16]))

        snap_id = storage.save_seller_snapshot(
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            cord_products_total=len(seen),
            rows=[
                {
                    "seller_id": sid,
                    "seller_name": names.get(sid, sid),
                    "cord_products": counts[sid],
                    "cord_categories": len(seller_cats[sid]),
                    "vtex_products": vtex_totals.get(sid),
                }
                for sid in counts
            ],
        )
        storage.close()
        print(f"[sellers] snapshot #{snap_id} guardado")
        return snap_id
