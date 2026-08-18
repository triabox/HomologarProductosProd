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
    track TEXT,
    venta REAL,          -- Venta S/ de 6 meses (el rank ordena por esta columna)
    unidades INTEGER
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    published INTEGER NOT NULL,
    in_stock INTEGER,
    found_name TEXT,
    found_seller TEXT,
    cord_url TEXT,
    vtex_published INTEGER
);
CREATE INDEX IF NOT EXISTS idx_checks_sku ON checks(sku);
CREATE TABLE IF NOT EXISTS data_summary (
    track TEXT, field TEXT, ok INTEGER, total INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS data_fails (
    rank INTEGER, sku TEXT, name TEXT, track TEXT, field TEXT,
    cord_value TEXT, vtex_value TEXT, updated_at TEXT
);
"""


def _migrate(db: sqlite3.Connection) -> None:
    cols = {r[1] for r in db.execute("PRAGMA table_info(checks)")}
    if "vtex_published" not in cols:
        db.execute("ALTER TABLE checks ADD COLUMN vtex_published INTEGER")
    scols = {r[1] for r in db.execute("PRAGMA table_info(skus)")}
    for col, typ in (("venta", "REAL"), ("unidades", "INTEGER")):
        if col not in scols:
            db.execute(f"ALTER TABLE skus ADD COLUMN {col} {typ}")


def _num(x) -> "float | None":
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


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
        self.exclude_sellers = [
            s.lower() for s in (cfg.get("topventas.exclude_sellers") or [])
        ]

    def _excluded(self, seller: str) -> bool:
        s = (seller or "").lower()
        return any(x in s for x in self.exclude_sellers)

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
                venta = _num(row.get("Venta S/"))
                unidades = _num(row.get("Unidades"))
                seller = row.get("Seller")
                track = "excluido" if self._excluded(seller) else _track(seller)
                db.execute(
                    """INSERT INTO skus(sku, rank, name, seller, track, venta, unidades)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(sku) DO UPDATE SET
                           venta=excluded.venta, unidades=excluded.unidades,
                           track=excluded.track""",
                    (sku, int(row.get("#") or 0), row.get("Producto"),
                     seller, track,
                     venta, int(unidades) if unidades is not None else None),
                )
                n += 1
        db.commit()
        return n

    # -- selección de muestra ---------------------------------------------
    def _pick_sample(
        self, db: sqlite3.Connection,
        track: "str | None" = None, all_pending: bool = False,
        recheck_missing: bool = False,
    ) -> list[sqlite3.Row]:
        if recheck_missing:
            # re-verificar los FALTANTES actuales (para depurar falsos positivos
            # causados por errores de API durante barridos masivos)
            cond = "AND s.track=?" if track else "AND s.track != 'excluido'"
            params = (track,) if track else ()
            return db.execute(
                f"""WITH last AS (SELECT c.*, ROW_NUMBER() OVER
                        (PARTITION BY c.sku ORDER BY c.checked_at DESC) rn FROM checks c)
                    SELECT s.*, l.checked_at AS last_check
                    FROM skus s JOIN last l ON l.sku=s.sku AND l.rn=1
                    WHERE l.published=0
                      AND (l.vtex_published IS NULL OR l.vtex_published=1) {cond}
                    ORDER BY s.venta DESC""",
                params,
            ).fetchall()
        if all_pending:
            # barrido completo: TODOS los nunca verificados (de la pista, o de todas),
            # por rank (los que más venden primero, para tener el % útil cuanto antes)
            cond = "AND s.track=?" if track else "AND s.track != 'excluido'"
            params = (track,) if track else ()
            return db.execute(
                f"""SELECT s.*, NULL AS last_check FROM skus s
                    WHERE NOT EXISTS (SELECT 1 FROM checks c WHERE c.sku = s.sku) {cond}
                    ORDER BY s.rank""",
                params,
            ).fetchall()
        picked: list[sqlite3.Row] = []
        samples = {track: self.samples.get(track, 100)} if track else self.samples
        for track, size in samples.items():
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
    async def _vtex_published(self, api: CordApi, sku: str) -> "int | None":
        """¿El SKU está publicado HOY en VTEX? (productos de temporada salen del catálogo).

        None = inconcluso (las requests fallaron): no confundir con "no está".
        """
        import json as _json
        base = self.cfg.get("vtex.base_url").rstrip("/")
        got_answer = False
        for field in ("skuId", "productId"):
            text = await api.http.get_text(
                f"{base}/api/catalog_system/pub/products/search?fq={field}:{sku}"
            )
            if text:
                try:
                    if _json.loads(text):
                        return 1
                    got_answer = True  # respuesta válida vacía: no está por este campo
                except _json.JSONDecodeError:
                    pass
        return 0 if got_answer else None

    async def _check(self, api: CordApi, row: sqlite3.Row) -> dict:
        sku, name, track = row["sku"], row["name"] or "", row["track"]
        result = {"sku": sku, "published": 0, "in_stock": None,
                  "found_name": None, "found_seller": None, "cord_url": None,
                  "vtex_published": None}
        # 1º: ¿sigue publicado en VTEX? (si no, no cuenta como faltante de CoRD)
        result["vtex_published"] = await self._vtex_published(api, sku)

        api_failed = False

        async def search(q: str, size: int = 8):
            nonlocal api_failed
            data = await api._get(f"/search/v3/products?q={q}&size={size}")
            if data is None:
                # error/rate-limit de la API: NO es lo mismo que "sin resultados"
                api_failed = True
                return []
            return data.get("items") or []

        candidates = []
        if track == "oechsle":
            candidates = await search(sku, size=5)
        if not candidates:
            words = "%20".join(norm_text(name).split()[:8])
            if words:
                candidates = await search(words)
        if not candidates and api_failed:
            # sin candidatos pero con fallos de API: inconcluso, no registrar
            # (el SKU queda pendiente y se reintenta en la próxima corrida)
            result["published"] = None
            return result

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
    async def run(self, track: "str | None" = None, all_pending: bool = False,
                  recheck_missing: bool = False) -> None:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        _migrate(db)
        total = self._load_skus(db)
        sample = self._pick_sample(db, track=track, all_pending=all_pending,
                                   recheck_missing=recheck_missing)
        modo = ("re-verificación de faltantes" if recheck_missing
                else "barrido completo" if all_pending else "muestra")
        print(f"[topventas] listado: {total} SKUs · {modo}"
              f"{' (' + track + ')' if track else ''} de esta corrida: {len(sample)}")

        async with HttpClient(self.cfg) as http:
            http.cache_enabled = False
            api = CordApi(self.cfg, http)
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            CHUNK = 12
            done = 0
            inconclusos = 0
            for i in range(0, len(sample), CHUNK):
                results = await asyncio.gather(
                    *(self._check(api, r) for r in sample[i:i + CHUNK])
                )
                for r in results:
                    if r["published"] is None:
                        inconclusos += 1  # error de API: queda pendiente, no se registra
                        continue
                    db.execute(
                        "INSERT INTO checks(sku, checked_at, published, in_stock, "
                        "found_name, found_seller, cord_url, vtex_published) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (r["sku"], now, r["published"], r["in_stock"],
                         r["found_name"], r["found_seller"], r["cord_url"],
                         r["vtex_published"]),
                    )
                db.commit()
                done += len(results)
                if done % 60 == 0:
                    print(f"[topventas]   {done}/{len(sample)} verificados")
            if inconclusos:
                print(f"[topventas] {inconclusos} chequeos inconclusos por errores de API "
                      f"(quedan pendientes para reintentar)")

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
               WHERE s.track != 'excluido'
               GROUP BY s.track"""
        ):
            pub = r["publicados"] or 0
            ver = r["verificados"] or 0
            pct = f"{pub/ver*100:.1f}%" if ver else "—"
            print(f"  {r['track']:12} verificados {ver}/{r['total']} · "
                  f"publicados {pub} ({pct}) · sin stock {r['sin_stock'] or 0}")
        db.close()

    # -- validación de DATOS de los top publicados --------------------------
    async def validate_published_data(self) -> None:
        """Corre la comparación completa (precios/nombre/etc.) sobre los top de venta
        publicados en CoRD y persiste el resultado para la sección del reporte."""
        import collections
        from .cord_scraper import CordScraper, permalink_from_url
        from .engine import Engine
        from .vtex_client import VtexClient

        eng = Engine(self.cfg)
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        db.executescript(_SCHEMA)
        _migrate(db)
        rows = db.execute("""
            WITH last AS (SELECT c.*, ROW_NUMBER() OVER
                (PARTITION BY sku ORDER BY checked_at DESC) rn FROM checks c)
            SELECT s.sku, s.rank, s.name, s.track, l.cord_url
            FROM skus s JOIN last l ON l.sku=s.sku AND l.rn=1
            WHERE l.published=1 AND l.cord_url IS NOT NULL
              AND s.track != 'excluido' ORDER BY s.rank""").fetchall()
        print(f"[topventas:datos] validando datos de {len(rows)} top publicados...")

        agg = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, 0]))
        fails = []
        async with HttpClient(self.cfg) as http:
            scraper = CordScraper(self.cfg, http)
            vc = VtexClient(self.cfg, http)

            async def one(r):
                try:
                    cord = await scraper.fetch_product(r["cord_url"], r["sku"])
                    if cord is None:
                        return None
                    if r["track"] == "oechsle":
                        vp = await vc.get_by_sku(r["sku"])
                    else:
                        vp = (await vc.get_by_ean(cord.ean, prefer_seller=cord.seller,
                                                  expected_name=cord.name)
                              if cord.ean else None)
                        if vp is None:
                            vp = await vc.get_by_slug(permalink_from_url(r["cord_url"]),
                                                      prefer_seller=cord.seller)
                    return (r, eng.compare(r["sku"], cord, vp, cord_url=r["cord_url"]))
                except Exception:
                    return None

            done = 0
            for i in range(0, len(rows), 12):
                out = await asyncio.gather(*(one(r) for r in rows[i:i + 12]))
                for x in out:
                    if not x:
                        continue
                    r, comp = x
                    if not comp.vtex_found:
                        continue
                    for fr in comp.fields:
                        if fr.severity.value == "NO_APLICA":
                            continue
                        a = agg[r["track"]][fr.field]
                        a[1] += 1
                        if fr.ok:
                            a[0] += 1
                        elif fr.field.startswith("precio"):
                            fails.append((r["rank"], r["sku"], r["name"], r["track"],
                                          fr.field, fr.cord_value, fr.vtex_value))
                done += len(rows[i:i + 12])
                if done % 240 == 0:
                    print(f"[topventas:datos]   {done}/{len(rows)}")

        now = time.strftime("%Y-%m-%d %H:%M")
        db.execute("DELETE FROM data_summary")
        db.execute("DELETE FROM data_fails")
        for track, fields in agg.items():
            for field, (ok, total) in fields.items():
                db.execute("INSERT INTO data_summary VALUES (?,?,?,?,?)",
                           (track, field, ok, total, now))
        for f in sorted(fails):
            db.execute("INSERT INTO data_fails VALUES (?,?,?,?,?,?,?,?)", (*f, now))
        db.commit()
        print(f"[topventas:datos] {len(fails)} fallos de precio en top publicados")
        self.render_report(db)
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
                   SUM(CASE WHEN l.published=1 AND l.in_stock=0 THEN 1 ELSE 0 END) ns,
                   SUM(s.venta) venta_total,
                   SUM(CASE WHEN l.sku IS NOT NULL THEN s.venta ELSE 0 END) venta_ver,
                   -- base VIVA y publicada: solo productos que SIGUEN en VTEX
                   SUM(CASE WHEN l.sku IS NOT NULL AND (l.vtex_published IS NULL
                       OR l.vtex_published=1) THEN s.venta ELSE 0 END) venta_viva,
                   SUM(CASE WHEN l.published=1 AND (l.vtex_published IS NULL
                       OR l.vtex_published=1) THEN s.venta ELSE 0 END) venta_pub,
                   SUM(CASE WHEN l.published=0 AND (l.vtex_published IS NULL
                       OR l.vtex_published=1) THEN s.venta ELSE 0 END) venta_falta,
                   SUM(CASE WHEN l.vtex_published=0 THEN s.venta ELSE 0 END) venta_novtex
            FROM skus s LEFT JOIN last l ON l.sku=s.sku AND l.rn=1
            WHERE s.track != 'excluido'
            GROUP BY s.track"""
        ):
            ver = r["verified"] or 0
            vv, vp = r["venta_ver"] or 0, r["venta_pub"] or 0
            # base VIVA: solo lo que sigue publicado en VTEX
            # (temporada/descontinuados no cuentan — no son faltantes de CoRD)
            viva = r["venta_viva"] or 0
            tracks.append({
                "key": r["track"],
                "label": labels.get(r["track"], r["track"]),
                "total": r["total"], "verified": ver,
                "cov_pct": round(ver / r["total"] * 100, 1) if r["total"] else 0,
                "pub_pct": round((r["pub"] or 0) / ver * 100, 1) if ver else 0,
                "no_stock": r["ns"] or 0,
                # dimensión PLATA (Venta S/ 6 meses del listado)
                "venta_total": r["venta_total"] or 0,
                "venta_ver": vv,
                "venta_cov_pct": round(vv / r["venta_total"] * 100, 1) if r["venta_total"] else 0,
                "venta_viva": viva,
                "venta_pub_pct": round(vp / viva * 100, 1) if viva else 0,
                "venta_falta": r["venta_falta"] or 0,
                "venta_novtex": r["venta_novtex"] or 0,
            })
        # FALTANTE real = está en VTEX (o aún sin verificar VTEX) y no en CoRD
        missing = [dict(x) for x in db.execute(
            LAST + """
            SELECT s.rank, s.sku, s.name, s.seller, s.track, s.venta, l.checked_at
            FROM skus s JOIN last l ON l.sku=s.sku AND l.rn=1
            WHERE l.published=0 AND (l.vtex_published IS NULL OR l.vtex_published=1)
              AND s.track != 'excluido'
            ORDER BY s.rank"""
        )]
        # no está en VTEX (temporada/descontinuado): informativo, NO es error
        not_in_vtex = [dict(x) for x in db.execute(
            LAST + """
            SELECT s.rank, s.sku, s.name, s.seller, s.track,
                   l.published AS cord_published
            FROM skus s JOIN last l ON l.sku=s.sku AND l.rn=1
            WHERE l.vtex_published=0 AND s.track != 'excluido' ORDER BY s.rank"""
        )]
        no_stock = [dict(x) for x in db.execute(
            LAST + """
            SELECT s.rank, s.sku, s.name, l.found_seller, l.cord_url
            FROM skus s JOIN last l ON l.sku=s.sku AND l.rn=1
            WHERE l.published=1 AND l.in_stock=0 AND s.track != 'excluido'
            ORDER BY s.rank"""
        )]
        published = [dict(x) for x in db.execute(
            LAST + """
            SELECT s.rank, s.sku, s.name, s.track, l.found_seller, l.in_stock, l.cord_url
            FROM skus s JOIN last l ON l.sku=s.sku AND l.rn=1
            WHERE l.published=1 AND s.track != 'excluido' ORDER BY s.rank"""
        )]
        total_skus = db.execute(
            "SELECT COUNT(*) FROM skus WHERE track != 'excluido'").fetchone()[0]
        # sección de calidad de datos (si se corrió --datos)
        datos = None
        try:
            ds = db.execute("SELECT * FROM data_summary").fetchall()
            if ds:
                labels = {"precio_venta": "Precio venta", "precio_promocional": "Precio promo",
                          "precio_sip": "Precio SIP", "name": "Nombre",
                          "description": "Descripción", "attributes": "Atributos",
                          "variants": "Variantes"}
                order = list(labels)
                by_track = {}
                for r in ds:
                    by_track.setdefault(r["track"], {})[r["field"]] = (r["ok"], r["total"])
                datos = {
                    "updated_at": ds[0]["updated_at"],
                    "order": order, "labels": labels, "by_track": by_track,
                    "fails": [dict(x) for x in db.execute(
                        "SELECT * FROM data_fails ORDER BY rank")],
                }
        except sqlite3.OperationalError:
            pass
        html = env.get_template("topventas.html.j2").render(
            datos=datos,
            total_skus=total_skus,
            generated_at=time.strftime("%Y-%m-%d %H:%M"),
            tracks=tracks, missing=missing, no_stock=no_stock, published=published,
            not_in_vtex=not_in_vtex,
        )
        out = self.cfg.root / "reports" / "topventas.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"[topventas] reporte: {out}")
        return out
