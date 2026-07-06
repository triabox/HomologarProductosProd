"""CLI del homologador.

Comandos:
  run     Corrida de homologación (con presupuesto de tiempo y reanudación) + reporte.
  seed    Compara un único producto por URL de CoRD (verificación rápida).
  report  Regenera el dashboard de una corrida y la página de tendencias.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys

from .config import Config
from .cord_scraper import CordScraper
from .engine import Engine
from .http import HttpClient
from .report import render_index, render_run, render_trends
from .scheduler import Runner, RunOptions
from .storage import Storage
from .vtex_client import VtexClient

_SKU_RE = re.compile(r"-(\d{5,})/p")


def _cfg(args) -> Config:
    return Config.load(args.config)


async def _cmd_run(args) -> int:
    cfg = _cfg(args)
    runner = Runner(cfg)
    opts = RunOptions(
        max_runtime_min=args.max_runtime,
        resume=not args.no_resume,
        only_category=args.category,
        limit_categories=args.limit_categories,
        no_cache=args.no_cache,
        counts_only=args.counts_only,
    )
    run_id = await runner.run(opts)
    storage = Storage(cfg.path("paths.db"))
    render_run(cfg, storage, run_id)
    render_trends(cfg, storage)
    index = render_index(cfg, storage)
    storage.close()
    print(f"\nPanel principal: {index}")
    return 0


async def _cmd_seed(args) -> int:
    cfg = _cfg(args)
    url = args.url
    sku = args.sku or (_SKU_RE.search(url).group(1) if _SKU_RE.search(url) else None)
    if not sku:
        print("No se pudo determinar el SKU; pasá --sku.", file=sys.stderr)
        return 2
    eng = Engine(cfg)
    async with HttpClient(cfg) as http:
        if args.no_cache:
            http.cache_enabled = False
        cord = await CordScraper(cfg, http).fetch_product(url, sku)
        if cord is None:
            print(f"No se pudo scrapear CoRD: {url}", file=sys.stderr)
            return 1
        vtex = await VtexClient(cfg, http).get_by_sku(sku)
    comp = eng.compare(sku, cord, vtex, cord_url=url)
    print(f"\nSKU {sku} · {cord.name}")
    print(f"vtex_found={comp.vtex_found} · SCORE={comp.score}\n")
    for fr in comp.fields:
        flag = "OK " if fr.ok else "XX "
        print(f"  {flag}{fr.field:12} {fr.score:.2f} [{fr.severity.value}] {fr.detail}")
        if not fr.ok:
            print(f"        CoRD={fr.cord_value!r}  VTEX={fr.vtex_value!r}")
    return 0


def _cmd_report(args) -> int:
    cfg = _cfg(args)
    storage = Storage(cfg.path("paths.db"))
    run_id = args.run_id or storage.last_finished_run_id()
    if not run_id:
        print("No hay corridas finalizadas.", file=sys.stderr)
        return 1
    render_run(cfg, storage, run_id)
    render_trends(cfg, storage)
    index = render_index(cfg, storage)
    storage.close()
    print(f"Panel principal: {index}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homologador", description=__doc__)
    parser.add_argument("--config", default=None, help="ruta a config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="corrida de homologación + reporte")
    p_run.add_argument("--max-runtime", type=float, default=None,
                       help="presupuesto de tiempo en minutos (corte limpio)")
    p_run.add_argument("--no-resume", action="store_true",
                       help="ignorar el cursor y procesar todas las categorías")
    p_run.add_argument("--category", default=None,
                       help="filtrar por nombre de categoría (substring)")
    p_run.add_argument("--limit-categories", type=int, default=None,
                       help="tope de categorías a procesar")
    p_run.add_argument("--no-cache", action="store_true", help="desactivar caché HTTP")
    p_run.add_argument("--counts-only", action="store_true",
                       help="solo comparar conteos por categoría (barrido rápido, sin validar productos)")

    p_seed = sub.add_parser("seed", help="comparar un único producto por URL de CoRD")
    p_seed.add_argument("url", help="URL de producto en CoRD")
    p_seed.add_argument("--sku", default=None, help="SKU (si no se infiere de la URL)")
    p_seed.add_argument("--no-cache", action="store_true")

    p_rep = sub.add_parser("report", help="regenerar dashboard/tendencias")
    p_rep.add_argument("--run-id", type=int, default=None)

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return asyncio.run(_cmd_run(args))
    if args.cmd == "seed":
        return asyncio.run(_cmd_seed(args))
    if args.cmd == "report":
        return _cmd_report(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
