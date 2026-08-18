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
from .export import export_excel, export_pdf
from .http import HttpClient
from .report import render_index, render_run, render_sellers, render_trends
from .scheduler import Runner, RunOptions
from .storage import Storage
from .vtex_client import VtexClient

_SKU_RE = re.compile(r"-(\d{5,})/p")


def _cfg(args) -> Config:
    cfg = Config.load(args.config)
    provider = getattr(args, "provider", None)
    if provider:
        cfg.apply_provider(provider)
    return cfg


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
    _export_latest(cfg, storage, index)
    storage.close()
    print(f"\nPanel principal: {index}")
    return 0


def _export_latest(cfg: Config, storage: Storage, index_path) -> None:
    """Genera export.xlsx (datos completos) y dashboard.pdf (formato web) del último run."""
    last = storage.last_finished_run_id()
    if not last:
        return
    out_dir = cfg.path("paths.reports_dir")
    try:
        export_excel(cfg, storage, last, out_dir / "export.xlsx")
        print(f"Excel:   {out_dir / 'export.xlsx'}")
    except Exception as e:
        print(f"[export] Excel falló: {type(e).__name__}: {e}")
    pdf = export_pdf(index_path, out_dir / "dashboard.pdf")
    if pdf:
        print(f"PDF:     {pdf}")


async def _cmd_seed(args) -> int:
    cfg = _cfg(args)
    url = args.url
    sku = args.sku or (_SKU_RE.search(url).group(1) if _SKU_RE.search(url) else None)
    if not sku:
        # productos de sellers: sin id numérico en la URL; usar el permalink como id
        from .cord_scraper import permalink_from_url
        sku = permalink_from_url(url) or ""
    if not sku:
        print("No se pudo determinar el SKU; pasá --sku.", file=sys.stderr)
        return 2
    eng = Engine(cfg)
    prov = cfg.provider
    async with HttpClient(cfg) as http:
        if args.no_cache:
            http.cache_enabled = False
        cord = await CordScraper(cfg, http).fetch_product(url, sku)
        if cord is None:
            print(f"No se pudo scrapear CoRD: {url}", file=sys.stderr)
            return 1
        vc = VtexClient(cfg, http)
        if prov.get("matching") == "ean":
            prefer = cord.seller if prov.get("vtex_seller") == "*" else prov.get("vtex_seller")
            vtex = (await vc.get_by_ean(cord.ean, prefer_seller=prefer,
                                        expected_name=cord.name)
                    if cord.ean else None)
            if vtex is None:
                from .cord_scraper import permalink_from_url
                vtex = await vc.get_by_slug(permalink_from_url(url), prefer_seller=prefer)
        else:
            vtex = await vc.get_by_sku(sku)
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
    _export_latest(cfg, storage, index)
    storage.close()
    print(f"Panel principal: {index}")
    return 0


async def _cmd_sellers(args) -> int:
    from .sellers import SellersAudit
    cfg = _cfg(args)
    await SellersAudit(cfg).run()
    storage = Storage(cfg.path("paths.db"))
    out = render_sellers(cfg, storage)
    storage.close()
    if out:
        print(f"Reporte sellers: {out}")
    return 0


def _cmd_export(args) -> int:
    cfg = _cfg(args)
    storage = Storage(cfg.path("paths.db"))
    run_id = args.run_id or storage.last_finished_run_id()
    if not run_id:
        print("No hay corridas finalizadas.", file=sys.stderr)
        return 1
    out_dir = cfg.path("paths.reports_dir")
    if args.format in ("xlsx", "both"):
        p = export_excel(cfg, storage, run_id, out_dir / f"export-run-{run_id}.xlsx")
        print(f"Excel: {p}")
    if args.format in ("pdf", "both"):
        index = render_index(cfg, storage)
        p = export_pdf(index, out_dir / f"dashboard-run-{run_id}.pdf")
        if p:
            print(f"PDF:   {p}")
    storage.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homologador", description=__doc__)
    parser.add_argument("--config", default=None, help="ruta a config.yaml")
    parser.add_argument("--provider", default=None,
                        choices=["oechsle", "plazavea", "marketplace"],
                        help="pista de validación (default: oechsle)")
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

    sub.add_parser("sellers", help="auditar sellers CoRD vs VTEX (corrida separada)")

    p_tv = sub.add_parser("topventas", help="verificar publicación en CoRD de los SKUs top de venta (solo local)")
    p_tv.add_argument("--datos", action="store_true",
                      help="validar los DATOS (precios/nombre/etc.) de los top publicados")
    p_tv.add_argument("--track", default=None,
                      choices=["oechsle", "plazavea", "marketplace"],
                      help="limitar a una pista")
    p_tv.add_argument("--all", action="store_true", dest="all_pending",
                      help="barrido completo: verificar TODOS los SKUs nunca verificados")
    p_tv.add_argument("--missing", action="store_true", dest="recheck_missing",
                      help="re-verificar los faltantes actuales (depura falsos positivos)")

    p_srv = sub.add_parser("serve", help="servir el dashboard con corridas a demanda")
    p_srv.add_argument("--port", type=int, default=int(__import__("os").environ.get("PORT", 8080)))

    p_exp = sub.add_parser("export", help="exportar resultados a Excel/PDF")
    p_exp.add_argument("--run-id", type=int, default=None, help="corrida (default: última)")
    p_exp.add_argument("--format", choices=["xlsx", "pdf", "both"], default="both")

    args = parser.parse_args(argv)
    if args.cmd == "run":
        return asyncio.run(_cmd_run(args))
    if args.cmd == "seed":
        return asyncio.run(_cmd_seed(args))
    if args.cmd == "report":
        return _cmd_report(args)
    if args.cmd == "sellers":
        return asyncio.run(_cmd_sellers(args))
    if args.cmd == "topventas":
        from .topventas import TopVentas
        tv = TopVentas(_cfg(args))
        if getattr(args, "datos", False):
            return asyncio.run(tv.validate_published_data()) or 0
        return asyncio.run(tv.run(
            track=getattr(args, "track", None),
            all_pending=getattr(args, "all_pending", False),
            recheck_missing=getattr(args, "recheck_missing", False),
        )) or 0
    if args.cmd == "serve":
        from .webserver import main as serve_main
        cfg = _cfg(args)
        serve_main(args.port, str(cfg.path("paths.reports_dir")))
        return 0
    if args.cmd == "export":
        return _cmd_export(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
