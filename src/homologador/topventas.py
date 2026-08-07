"""Top Ventas: verifica que los SKUs que concentran la venta estén publicados en CoRD.

Proceso LOCAL y aparte (`homologador topventas`): lee data/top_ventas.csv (listado
del 90% de la venta — NO se versiona ni se sube al servidor), toma una muestra
aleatoria por pista (Oechsle / Plaza Vea / Marketplace) priorizando SKUs nunca
verificados, y chequea publicación + stock en CoRD vía su búsqueda `q=`:

- Oechsle (1P/SFS): q=<ID_SKU> — el id numérico está indexado; verifica nombre.
- Plaza Vea / Marketplace: q=<nombre> con verificación de similitud y de que el
  seller hallado pertenezca a la misma pista.

Resultados en data/homologador-topventas.db + reporte reports/topventas.html.
"""
from __future__ import annotations

import asyncio
import csv
import random
import sqlite3
import time
from pathlib import Path

from rapidfuzz import fuzz

from .config import Config
from .cord_api import CordApi
from .http import HttpClient
from .normalize import norm_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skus (
    sku TEXT PRIMARY KEY,
    rank INTEGER,
    name TEXT,
    seller TEXT,
    track TEXT
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    published INTEGER NOT NULL,
    in_stock INTEGER,
    found_name TEXT,
    found_seller TEXT,
    cord_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_sku ON checks(sku);
"""


def _track(seller: str) -> str:
    s = (seller or "").lower()
    if s.startswith("oechsle"):
        return "oechsle"
    if "plaza" in s:
        return "plazavea"
    return "marketplace"


def _item_track(seller_id: str) -> str:
    return _track(seller_id or "")


class TopVentas:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.csv_path = cfg.root / "data" / "top_ventas.csv"
        self.db_path = cfg.root / "data" / "homologador-topventas.db"
        self.sim_threshold = cfg.get("topventas.name_threshold", 70)
        self.samples = cfg.get("topventas.sample") or {
            "oechsle": 100, "plazavea": 40, "marketplace": 60,
        }

    # -- carga del listado -------------------------------------------------
    def _load_skus(self, db: sqlite3.Connection) -> int:
        if not self.csv_path.exists():
            raise FileNotFoundError(
                f"no existe {self.csv_path} — este proceso corre solo donde esté el listado"
            )
        n = 0
        with open(self.csv_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                sku = (row.get("ID_SKU") or "").strip()
                if not sku:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO skus(sku, rank, name, seller, track) "
                    "VALUES (?,?,?,?,?)",
                    (sku, int(row.get("#") or 0), row.get("Producto"),
                     row.get("Seller"), _track(row.get("Seller"))),
                )
                n += 1
        db.commit()
        return n

    # -- selección de muestra ---------------------------------------------
    def _pick_sample(self, db: sqlite3.Connection) -> list[sqlite3.Row]:
        picked: list[sqlite3.Row] = []
        for track, size in self.samples.items():
            rows = db.execute(
                """SELECT s.*, MAX(c.checked_at) AS last_check
                   FROM skus s LEFT JOIN checks c ON c.sku = s.sku
                   WHERE s.track = ?
                   GROUP BY s.sku
                   ORDER BY (last_check IS NOT NULL), last_check ASC
                   LIMIT ?""",
                (track, size * 3),
            ).fetchall()
            never = [r for r in rows if r["last_check"] is None]
            older = [r for r in rows if r["last_check"] is not None]
            random.shuffle(never)
            take = never[:size]
            if len(take) < size:
                take += older[: size - len(take)]
            picked += take
        return picked

    # -- verificación de un SKU -------------------------------------------
    async def _check(self, api: CordApi, row: sqlite3.Row) -> dict:
        sku, name, track = row["sku"], row["name"] or "", row["track"]
        result = {"sku": sku, "published": 0, "in_stock": None,
                  "found_name": None, "found_seller": None, "cord_url": None}

        async def search(q: str, size: int = 8):
            data = await api._get(f"/search/v3/products?q={q}&size={size}")
            return (data or {}).get("items") or []

        candidates = []
        if track == "oechsle":
            candidates = await search(sku, size=5)
        if not candidates:
            words = "%20".join(norm_text(name).split()[:8])
            if words:
                candidates = await search(words)

        best, best_score = None, -1
        for it in candidates:
            it_seller = ((it.get("skus") or [{}])[0].get("seller") or {}).get("sellerId") or ""
            if track == "oechsle":
                perma = (it.get("seo") or {}).get("permalink") or ""
                exact = str(it.get("productId")) == sku or perma.endswith(f"-{sku}")
            else:
                exact = False
                if _item_track(it_seller) != track:
                    continue  # el seller hallado no es de esta pista
            score = fuzz.token_set_ratio(norm_text(name), norm_text(it.get("name") or ""))
            if exact:
                score += 100  # match de id manda
            if score > best_score:
                best, best_score = it, score

        if best is not None and best_score >= self.sim_threshold:
            sku0 = (best.get("skus") or [{}])[0]
            seller = (sku0.get("seller") or {})
            perma = (best.get("seo") or {}).get("permalink") or ""
            result.update(
                published=1,
                in_stock=1 if (seller.get("availableUnits") or 0) > 0 else 0,
                found_name=(best.get("name") or "")[:90],
                found_seller=seller.get("sellerId"),
                cord_url=f"{api.site}/{perma}/p" if perma else None,
            )
        return result

    # -- corrida ------------------------------------------------------------
    async def run(self) -> None:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        total = self._load_skus(db)
        sample = self._pick_sample(db)
        print(f"[topventas] listado: {total} SKUs · muestra de esta corrida: {len(sample)}")

        async with HttpClient(self.cfg) as http:
            http.cache_enabled = False
            api = CordApi(self.cfg, http)
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            CHUNK = 12
            done = 0
            for i in range(0, len(sample), CHUNK):
                results = await asyncio.gather(
                    *(self._check(api, r) for r in sample[i:i + CHUNK])
                )
                for r in results:
                    db.execute(
                        "INSERT INTO checks(sku, checked_at, published, in_stock, "
                        "found_name, found_seller, cord_url) VALUES (?,?,?,?,?,?,?)",
                        (r["sku"], now, r["published"], r["in_stock"],
                         r["found_name"], r["found_seller"], r["cord_url"]),
                    )
                db.commit()
                done += len(results)
                if done % 60 == 0:
                    print(f"[topventas]   {done}/{len(sample)} verificados")

        self.render_report(db)
        # resumen por pista (última verificación de cada SKU)
        print("\n[topventas] estado acumulado por pista:")
        for r in db.execute(
            """WITH last AS (
                 SELECT c.sku, c.published, c.in_stock,
                        ROW_NUMBER() OVER (PARTITION BY c.sku ORDER BY c.checked_at DESC) rn
                 FROM checks c)
               SELECT s.track,
                      COUNT(DISTINCT s.sku) total,
                      COUNT(DISTINCT l.sku) verificados,
                      SUM(CASE WHEN l.published=1 THEN 1 ELSE 0 END) publicados,
                      SUM(CASE WHEN l.published=1 AND l.in_stock=0 THEN 1 ELSE 0 END) sin_stock
               FROM skus s LEFT JOIN last l ON l.sku=s.sku AND l.rn=1
               GROUP BY s.track"""
        ):
            pub = r["publicados"] or 0
            ver = r["verificados"] or 0
            pct = f"{pub/ver*100:.1f}%" if ver else "—"
            print(f"  {r['track']:12} verificados {ver}/{r['total']} · "
                  f"publicados {pub} ({pct}) · sin stock {r['sin_stock'] or 0}")
        db.close()

    # -- reporte HTML -------------------------------------------------------
    def render_report(self, db: sqlite3.Connection) -> Path:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        env = Environment(
            loader=FileSystemLoader(str(self.cfg.root / "templates")),
            autoescape=select_autoescape(["html"]),
        )
        LAST = """WITH last AS (
            SELECT c.*, ROW_NUMBER() OVER (PARTITION BY c.sku ORDER BY c.checked_at DESC) rn
            FROM checks c)"""
        labels = {"oechsle": "Oechsle", "plazavea": "Plaza Vea", "marketplace": "Marketplace"}
        tracks = []
        for r in db.execute(
            LAST + """
            SELECT s.track, COUNT(DISTINCT s.sku) total, COUNT(DISTINCT l.sku) verified,
                   SUM(CASE WHEN l.published=1 THEN 1 ELSE 0 END) pub,
                   SUM(CASE WHEN l.published=1 AND l.in_stock=0 THEN 1 ELSE 0 END) ns
            FROM skus s LEFT JOIN last l ON l.sku=s.sku AND l.rn=1
            GROUP BY s.track"""
        ):
            ver = r["verified"] or 0
            tracks.append({
                "label": labels.get(r["track"], r["track"]),
                "total": r["total"], "verified": ver,
                "cov_pct": round(ver / r["total"] * 100, 1) if r["total"] else 0,
                "pub_pct": round((r["pub"] or 0) / ver * 100, 1) if ver else 0,
                "no_stock": r["ns"] or 0,
            })
        missing = [dict(x) for x in db.execute(
            LAST + """
            SELECT s.rank, s.sku, s.name, s.seller, s.track, l.checked_at
            FROM skus s JOIN last l ON l.sku=s.sku AND l.rn=1
            WHERE l.published=0 ORDER BY s.rank"""
        )]
        no_stock = [dict(x) for x in db.execute(
            LAST + """
            SELECT s.rank, s.sku, s.name, l.found_seller, l.cord_url
            FROM skus s JOIN last l ON l.sku=s.sku AND l.rn=1
            WHERE l.published=1 AND l.in_stock=0 ORDER BY s.rank"""
        )]
        published = [dict(x) for x in db.execute(
            LAST + """
            SELECT s.rank, s.sku, s.name, s.track, l.found_seller, l.in_stock, l.cord_url
            FROM skus s JOIN last l ON l.sku=s.sku AND l.rn=1
            WHERE l.published=1 ORDER BY s.rank"""
        )]
        total_skus = db.execute("SELECT COUNT(*) FROM skus").fetchone()[0]
        html = env.get_template("topventas.html.j2").render(
            total_skus=total_skus,
            generated_at=time.strftime("%Y-%m-%d %H:%M"),
            tracks=tracks, missing=missing, no_stock=no_stock, published=published,
        )
        out = self.cfg.root / "reports" / "topventas.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"[topventas] reporte: {out}")
        return out
