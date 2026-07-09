"""Runner con presupuesto de tiempo y reanudación por cursor.

Flujo de una corrida:
1. Trae el árbol de categorías (VTEX) con la PLP de CoRD derivada.
2. Selecciona categorías pendientes (si --resume, salta las ya hechas; si están todas
   hechas, reinicia el cursor para un nuevo ciclo de re-validación).
3. Por categoría, hasta agotar el presupuesto de tiempo:
   descubre productos en CoRD -> muestrea >=20 -> por cada uno: scrape CoRD + lookup
   VTEX + compara -> persiste resultados y marca la categoría como hecha (checkpoint).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .cord_scraper import CordScraper
from .config import Config
from .discovery import CordDiscovery
from .engine import Engine
from .http import HttpClient
from .matching import Sampler
from .models import Category, DiscoveredProduct, ProductComparison
from .normalize import norm_label
from .stats import aggregate_category, global_summary
from .storage import Storage
from .vtex_client import VtexClient


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunOptions:
    max_runtime_min: float | None = None   # None = sin límite
    resume: bool = True
    only_category: str | None = None       # filtra por nombre de categoría (substring)
    limit_categories: int | None = None    # tope de categorías a procesar
    no_cache: bool = False
    counts_only: bool = False              # solo comparar conteos (barrido rápido)


class Runner:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = Engine(cfg)
        self.sampler = Sampler(cfg)
        self.oechsle_id = str(cfg.get("cord.oechsle_seller_id", "oechsle")).lower()

    def _is_oechsle(self, dp: DiscoveredProduct) -> bool:
        return (dp.seller or "").lower() == self.oechsle_id

    def _oechsle_count(self, cord_total: "int | None", ssr_sellers: list[str]) -> "int | None":
        """Estima el total de productos de Oechsle en CoRD = total x ratio Oechsle del SSR.
        Exacto en categorías homogéneas (todo un vendedor)."""
        if cord_total is None:
            return None
        if cord_total == 0:
            return 0
        if not ssr_sellers:
            return None  # no se pudo determinar el vendedor
        oe = sum(1 for s in ssr_sellers if s.lower() == self.oechsle_id)
        return round(cord_total * oe / len(ssr_sellers))

    @staticmethod
    def _finalize(storage: Storage, run_id: int) -> None:
        """Cierra una corrida calculando totales desde lo ya guardado."""
        s = global_summary(storage, run_id)
        cats = len(storage.category_stats(run_id))
        storage.finish_run(
            run_id, _now_iso(), categories_total=cats, categories_done=cats,
            products_compared=s.vtex_found, avg_score=s.avg_score,
        )

    async def run(self, opts: RunOptions) -> int:
        storage = Storage(self.cfg.path("paths.db"))
        # recuperar corridas anteriores que quedaron colgadas (proceso cortado a mitad)
        for rid in storage.dangling_run_ids():
            self._finalize(storage, rid)
            print(f"[scheduler] corrida colgada #{rid} finalizada automáticamente")
        deadline = (
            time.monotonic() + opts.max_runtime_min * 60
            if opts.max_runtime_min
            else None
        )
        run_id = storage.start_run(
            _now_iso(),
            {
                "max_runtime_min": opts.max_runtime_min,
                "resume": opts.resume,
                "only_category": opts.only_category,
                "per_category": self.sampler.n,
            },
        )
        print(f"[run {run_id}] iniciada {_now_iso()}")

        # auto-aprendizaje de visibilidad de atributos (front de VTEX)
        self._auto_learn = self.cfg.get("comparators.attributes.auto_learn_visibility", False)
        self._vis_threshold = self.cfg.get("comparators.attributes.visibility_threshold", 2)
        self._decided = storage.get_decided_labels()
        attr_cmp = self.engine.registry.get("attributes")
        if attr_cmp is not None:
            attr_cmp.learned_exclude = storage.get_excluded_labels()

        cord_base = self.cfg.get("cord.base_url")
        async with HttpClient(self.cfg) as http:
            if opts.no_cache:
                http.cache_enabled = False
            vtex = VtexClient(self.cfg, http)
            scraper = CordScraper(self.cfg, http)
            discovery = CordDiscovery(self.cfg, http)

            # el árbol es crítico: si falla (vacío), reintentar antes de abortar
            tree = await vtex.get_category_tree(cord_base)
            if not tree:
                await asyncio.sleep(3)
                tree = await vtex.get_category_tree(cord_base)
            if not tree:
                storage.conn.execute("DELETE FROM runs WHERE id=?", (run_id,))
                storage.conn.commit()
                storage.close()
                print(f"[run {run_id}] árbol de categorías VTEX no disponible; corrida abortada")
                return run_id
            # modo conteo: barrido rápido de TODAS las categorías, sin validar productos
            if opts.counts_only:
                cats = tree
                if opts.only_category:
                    n = opts.only_category.lower()
                    cats = [c for c in cats if n in c.name.lower()
                            or any(n in x.lower() for x in c.name_path)]
                if opts.limit_categories:
                    cats = cats[: opts.limit_categories]
                await self._sweep_counts(storage, run_id, cats, discovery, vtex, deadline)
                storage.close()
                return run_id

            categories = self._select_categories(tree, storage, opts)
            print(f"[run {run_id}] {len(categories)} categorías a procesar")

            total_compared = 0
            score_sum = 0.0
            cats_done = 0
            hit_deadline = False

            while categories:
              offsets = storage.sample_offsets()  # offset de rotación por categoría
              for cat in categories:
                if deadline and time.monotonic() >= deadline:
                    print(f"[run {run_id}] presupuesto de tiempo agotado, corte limpio")
                    hit_deadline = True
                    break

                offset = offsets.get(cat.id, 0)
                comps, total_found, cord_count, vtex_count = await self._process_category(
                    cat, discovery, scraper, vtex, storage, run_id, offset
                )
                next_off = self.sampler.next_offset(offset, total_found)
                if not comps:
                    # sin productos en CoRD: igual registra el conteo (CoRD 0 vs VTEX N)
                    storage.save_category_stats(
                        run_id, cat.name, 0, 0, 0.0, {},
                        cord_url=cat.cord_url, vtex_url=cat.vtex_url,
                        cord_count=cord_count, vtex_count=vtex_count,
                    )
                    storage.mark_category_done(cat.id, cat.name, run_id, _now_iso())
                    print(f"  - {cat.path_str}: sin productos en CoRD "
                          f"(CoRD={cord_count} vs VTEX-Oechsle={vtex_count})")
                    continue

                agg = aggregate_category(cat.name, comps)
                storage.save_category_stats(
                    run_id, cat.name, agg.sampled, agg.vtex_found,
                    agg.avg_score, agg.field_ok,
                    cord_url=cat.cord_url, vtex_url=cat.vtex_url,
                    cord_count=cord_count, vtex_count=vtex_count,
                )
                storage.mark_category_done(
                    cat.id, cat.name, run_id, _now_iso(), sample_offset=next_off
                )
                cats_done += 1
                total_compared += agg.vtex_found
                score_sum += agg.avg_score * agg.vtex_found
                print(
                    f"  ✓ {cat.path_str}: {agg.sampled} muestreados, "
                    f"{agg.vtex_found} en VTEX, score {agg.avg_score}"
                )

              # ¿ciclo completado con presupuesto restante? -> reiniciar y seguir
              if hit_deadline or not opts.resume or not deadline or opts.limit_categories:
                  break
              if cats_done == 0:
                  # pasada completa sin UN solo producto: CoRD probablemente
                  # inaccesible/bloqueado -> no martillar con otro ciclo vacío
                  print(f"[run {run_id}] pasada sin productos (¿CoRD inaccesible?); "
                        f"corto en vez de reciclar")
                  break
              categories = self._select_categories(tree, storage, opts)
              if categories:
                  print(f"[run {run_id}] ciclo completado; continúa un ciclo nuevo con "
                        f"muestras rotadas ({len(categories)} categorías)")

            avg = round(score_sum / total_compared, 2) if total_compared else 0.0
            storage.finish_run(
                run_id, _now_iso(),
                categories_total=len(categories), categories_done=cats_done,
                products_compared=total_compared, avg_score=avg,
            )
            print(
                f"[run {run_id}] fin: {cats_done} categorías, {total_compared} productos, "
                f"score promedio {avg} | cache {http.stats}"
            )
        storage.close()
        return run_id

    def _select_categories(
        self, categories: list[Category], storage: Storage, opts: RunOptions
    ) -> list[Category]:
        if opts.only_category:
            needle = opts.only_category.lower()
            categories = [
                c for c in categories
                if needle in c.name.lower()
                or any(needle in n.lower() for n in c.name_path)
            ]
        if opts.resume:
            done = storage.done_category_ids()
            pending = [c for c in categories if c.id not in done]
            if not pending and categories:
                # ciclo completo terminado -> reiniciar para re-validar
                print("[scheduler] todas las categorías procesadas; reiniciando cursor")
                storage.reset_cursor()
                pending = categories
            categories = pending
        if opts.limit_categories:
            categories = categories[: opts.limit_categories]
        return categories

    async def _sweep_counts(
        self, storage: Storage, run_id: int, cats: list[Category],
        discovery: CordDiscovery, vtex: VtexClient, deadline: float | None,
    ) -> None:
        """Barrido rápido: solo conteo CoRD vs VTEX-Oechsle por categoría (sin validar productos)."""
        print(f"[run {run_id}] conteo de {len(cats)} categorías (modo counts-only)")

        async def one(cat: Category):
            _, cord_total, ssr_sellers = await discovery.discover_category(cat)
            vtex_count = await vtex.category_count(cat.id_path)
            return cat, self._oechsle_count(cord_total, ssr_sellers), vtex_count

        done = matched = anomalies = 0
        CHUNK = 16
        for i in range(0, len(cats), CHUNK):
            if deadline and time.monotonic() >= deadline:
                print(f"[run {run_id}] presupuesto agotado; {done}/{len(cats)} contadas")
                break
            results = await asyncio.gather(*(one(c) for c in cats[i:i + CHUNK]))
            for cat, cc, vc in results:
                storage.save_category_stats(
                    run_id, cat.name, 0, 0, 0.0, {},
                    cord_url=cat.cord_url, vtex_url=cat.vtex_url,
                    cord_count=cc, vtex_count=vc,
                )
                done += 1
                cc0, vc0 = cc or 0, vc or 0
                if cc0 == vc0:
                    matched += 1
                if cc0 > vc0:
                    anomalies += 1
            if done % 160 == 0 or i + CHUNK >= len(cats):
                print(f"[run {run_id}]   {done}/{len(cats)} categorías contadas")
        self._finalize(storage, run_id)
        print(f"[run {run_id}] fin conteos: {done} categorías, {matched} iguales, "
              f"{anomalies} anomalías (CoRD>VTEX)")

    async def _process_category(
        self,
        cat: Category,
        discovery: CordDiscovery,
        scraper: CordScraper,
        vtex: VtexClient,
        storage: Storage,
        run_id: int,
        offset: int = 0,
    ) -> tuple[list[ProductComparison], int, "int | None", "int | None"]:
        discovered, cord_total, ssr_sellers = await discovery.discover_category(cat)
        vtex_count = await vtex.category_count(cat.id_path)
        cord_count = self._oechsle_count(cord_total, ssr_sellers)  # solo Oechsle
        # validar solo los productos vendidos por Oechsle
        oechsle = [p for p in discovered if self._is_oechsle(p)]
        if not oechsle:
            return [], 0, cord_count, vtex_count
        sample = self.sampler.sample(oechsle, offset)

        async def one(dp: DiscoveredProduct) -> ProductComparison:
            try:
                cord = await scraper.fetch_product(dp.url, dp.sku)
                if cord is None:
                    return ProductComparison(
                        sku=dp.sku, category_name=cat.name, cord_url=dp.url,
                        vtex_url=None, vtex_found=False,
                        error="no se pudo scrapear CoRD",
                    )
                cord.category_name = cord.category_name or cat.name
                vtex_p = await vtex.get_by_sku(dp.sku)
                comp = self.engine.compare(dp.sku, cord, vtex_p, cord_url=dp.url)
                if self._auto_learn and vtex_p is not None:
                    await self._learn_visibility(comp, vtex_p, vtex, storage)
                return comp
            except Exception as e:  # un fallo no rompe la corrida
                return ProductComparison(
                    sku=dp.sku, category_name=cat.name, cord_url=dp.url,
                    vtex_url=None, vtex_found=False, error=f"{type(e).__name__}: {e}",
                )

        comps = await asyncio.gather(*(one(dp) for dp in sample))
        for comp in comps:
            storage.save_comparison(run_id, comp)
        storage.conn.commit()
        return list(comps), len(oechsle), cord_count, vtex_count

    async def _learn_visibility(self, comp, vtex_p, vtex, storage) -> None:
        """Si faltan atributos en CoRD, verifica el front de VTEX y aprende a descartar
        los que VTEX tampoco muestra al cliente (tras `visibility_threshold` confirmaciones)."""
        attr_fr = next((f for f in comp.fields if f.field == "attributes"), None)
        if attr_fr is None:
            return
        missing = attr_fr.extra.get("missing") or []
        # solo labels aún no resueltas (ni descartadas ni confirmadas visibles)
        undecided = [(lbl, norm_label(lbl)) for lbl in missing
                     if norm_label(lbl) not in self._decided]
        if not undecided or not vtex_p.url:
            return
        front = await vtex.get_front_spec_labels(vtex_p.url)
        if front is None:
            return
        attr_cmp = self.engine.registry.get("attributes")
        for orig, norm in undecided:
            is_visible = norm in front
            newly_excluded = storage.record_visibility(
                norm, orig, is_visible, self._vis_threshold
            )
            # confirmado visible o recién descartado -> ya está resuelto
            if is_visible or newly_excluded:
                self._decided.add(norm)
            if newly_excluded and attr_cmp is not None:
                attr_cmp.learned_exclude.add(norm)
