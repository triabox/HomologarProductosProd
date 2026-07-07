"""Generación del dashboard HTML (por corrida) y la página de tendencias (histórico).

Las columnas por comparador se generan dinámicamente desde los comparadores
registrados, de modo que agregar un comparador nuevo aparece solo en el reporte.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import Config
from .engine import Engine
from .stats import GlobalSummary, compute_deltas, global_summary
from .storage import Storage

_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def _duration(started_at: Optional[str], finished_at: Optional[str]) -> tuple[Optional[float], str]:
    """Duración de una corrida en segundos + texto legible (ej. '3m 58s')."""
    if not started_at or not finished_at:
        return None, "—"
    try:
        secs = (datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds()
    except ValueError:
        return None, "—"
    if secs < 60:
        return secs, f"{secs:.0f}s"
    m, s = divmod(int(secs), 60)
    if m < 60:
        return secs, f"{m}m {s}s"
    h, m = divmod(m, 60)
    return secs, f"{h}h {m}m"


def render_run(cfg: Config, storage: Storage, run_id: int) -> Path:
    """Renderiza el dashboard de una corrida y devuelve la ruta del HTML."""
    labels = Engine(cfg).comparator_labels()
    summary = global_summary(storage, run_id)
    prev_id = storage.last_finished_run_id(before_id=run_id)
    prev_summary: Optional[GlobalSummary] = (
        global_summary(storage, prev_id) if prev_id else None
    )
    deltas = compute_deltas(summary, prev_summary)

    run = storage.get_run(run_id)
    cats = [dict(r) for r in storage.category_stats(run_id)]
    for c in cats:
        c["field_ok"] = json.loads(c["field_ok_json"] or "{}")

    # detalle por producto: arma {sku -> {field -> resultado}} para mostrar diferencias
    fields_by_sku: dict[str, dict] = {}
    for fr in storage.field_results(run_id):
        fields_by_sku.setdefault(fr["sku"], {})[fr["field"]] = {
            "ok": bool(fr["ok"]),
            "severity": fr["severity"],
            "detail": fr["detail"],
            "cord_value": fr["cord_value"],
            "vtex_value": fr["vtex_value"],
        }

    products = []
    for p in storage.product_results(run_id):
        if not p["vtex_found"]:
            continue
        flds = fields_by_sku.get(p["sku"], {})
        products.append(
            {
                "sku": p["sku"],
                "category_name": p["category_name"],
                "score": p["score"],
                "cord_url": p["cord_url"],
                "vtex_url": p["vtex_url"],
                "fields": flds,
                "has_diff": any(not f["ok"] for f in flds.values()),
            }
        )

    not_found = [
        dict(r) for r in storage.product_results(run_id) if not r["vtex_found"]
    ]

    # atributos faltantes en CoRD (agregado por label)
    attr_missing = []
    for r in storage.attribute_gaps(run_id, "missing"):
        d = dict(r)
        skus = (d.pop("skus", "") or "").split(",")
        d["skus"] = skus
        d["sku_sample"] = skus[:25]
        d["sku_more"] = max(0, len(skus) - 25)
        attr_missing.append(d)
    attr_mismatch = [dict(r) for r in storage.attribute_gaps(run_id, "mismatch")]

    _, duration_str = _duration(run["started_at"], run["finished_at"])

    html = _env().get_template("run.html.j2").render(
        run=dict(run),
        duration=duration_str,
        summary=summary,
        deltas=deltas,
        labels=labels,
        field_keys=list(labels.keys()),
        categories=cats,
        products=products,
        not_found=not_found,
        attr_missing=attr_missing,
        attr_mismatch=attr_mismatch,
    )
    out_dir = cfg.path("paths.reports_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = (run["started_at"] or "").replace(":", "").replace("-", "")[:13]
    out = out_dir / f"run-{run_id}-{stamp}.html"
    out.write_text(html, encoding="utf-8")
    return out


def _status(value: float, goal: float) -> str:
    """Clasifica un valor contra su objetivo: ok / cerca / debajo."""
    if value >= goal:
        return "ok"
    if value >= goal - 3:
        return "cerca"
    return "debajo"


def render_index(cfg: Config, storage: Storage) -> Path:
    """Dashboard principal: estado vs objetivos, priorización y acceso a todos los reportes."""
    labels = Engine(cfg).comparator_labels()
    goals = cfg.get("goals", {}) or {}
    out_dir = cfg.path("paths.reports_dir")
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = storage.all_runs()
    if not runs:
        html = _env().get_template("index.html.j2").render(
            has_data=False, labels=labels, field_keys=list(labels.keys()), goals=goals,
        )
        out = out_dir / "index.html"
        out.write_text(html, encoding="utf-8")
        return out

    # asegura que exista el HTML de cada corrida y arma la lista del historial
    history = []
    for r in runs:
        path = render_run(cfg, storage, r["id"])
        s = global_summary(storage, r["id"])
        secs, dur_str = _duration(r["started_at"], r["finished_at"])
        rate = (
            round(s.products_compared / (secs / 60.0), 1)
            if secs and secs > 0 and s.products_compared
            else None
        )
        history.append(
            {
                "run_id": r["id"],
                "started_at": r["started_at"],
                "duration": dur_str,
                "rate": rate,  # productos/min
                "products": s.products_compared,
                "avg_score": s.avg_score,
                "coverage_pct": s.coverage_pct,
                "field_ok": s.field_ok,
                "file": path.name,
            }
        )
    history.reverse()  # más reciente primero

    latest_id = runs[-1]["id"]
    summary = global_summary(storage, latest_id)
    prev_id = storage.last_finished_run_id(before_id=latest_id)
    deltas = compute_deltas(summary, global_summary(storage, prev_id) if prev_id else None)

    # estado por campo vs objetivo
    field_status = []
    for key in labels:
        val = summary.field_ok.get(key, 0.0)
        goal = goals.get(key)
        field_status.append(
            {
                "key": key,
                "label": labels[key],
                "value": val,
                "goal": goal,
                "delta": deltas.field_ok.get(key),
                "status": _status(val, goal) if goal is not None else "ok",
                "gap": round(goal - val, 1) if goal is not None and val < goal else 0,
                "na": summary.na_counts.get(key, 0),
            }
        )

    # categorías a priorizar: impacto = productos x suma de brechas vs objetivos
    cats = []
    for c in storage.category_stats(latest_id):
        field_ok = json.loads(c["field_ok_json"] or "{}")
        deficits = {
            k: max(0.0, goals[k] - field_ok.get(k, 0.0)) for k in labels if k in goals
        }
        total_deficit = sum(deficits.values())
        n = c["vtex_found"] or 0
        priority = round(n * total_deficit / 100.0, 1)
        if total_deficit > 0:
            cats.append(
                {
                    "category_name": c["category_name"],
                    "products": n,
                    "field_ok": field_ok,
                    "deficits": deficits,
                    "priority": priority,
                    "cord_url": c["cord_url"],
                    "vtex_url": c["vtex_url"],
                }
            )
    cats.sort(key=lambda x: x["priority"], reverse=True)

    # proyección acumulada: % global por campo si se "arreglan" (llevan a 100%) las
    # categorías de la lista, de arriba hacia abajo. Muestra cuánto acerca al objetivo.
    total = summary.vtex_found or 0
    global_ok = {k: summary.field_ok.get(k, 0.0) / 100.0 * total for k in labels}
    cum_fixed = {k: 0.0 for k in labels}
    for c in cats:
        for k in labels:
            catpct = c["field_ok"].get(k)
            if catpct is not None:
                cum_fixed[k] += c["products"] * (1 - catpct / 100.0)
        c["projected"] = {
            k: (round(min(100.0, (global_ok[k] + cum_fixed[k]) / total * 100.0), 1)
                if total else None)
            for k in labels
        }

    # ---- worklists priorizadas: una por cada punto analizado (comparador) ----
    ITEM_LIMIT = 50
    attr_missing = []
    for r in storage.attribute_gaps(latest_id, "missing"):
        d = dict(r)
        d.pop("skus", None)
        attr_missing.append(d)

    worklists = []
    for fst in field_status:
        key = fst["key"]
        count = storage.failing_count(latest_id, key)
        block = {
            "key": key,
            "label": fst["label"],
            "value": fst["value"],
            "goal": fst["goal"],
            "gap": fst["gap"],
            "status": fst["status"],
            "count": count,
        }
        if key == "attributes":
            # worklist por atributo faltante (qué specs cargar, por impacto)
            block["kind"] = "attribute"
            block["rows"] = attr_missing[:ITEM_LIMIT]
            block["shown"] = min(len(attr_missing), ITEM_LIMIT)
            block["total_rows"] = len(attr_missing)
        else:
            # worklist por producto (peor primero)
            items = [dict(r) for r in storage.failing_items(latest_id, key, ITEM_LIMIT)]
            block["kind"] = "product"
            block["rows"] = items
            block["shown"] = len(items)
            block["total_rows"] = count
        worklists.append(block)

    # ordenar los bloques por brecha al objetivo (lo más lejos, primero)
    worklists.sort(key=lambda b: (b["gap"] or 0), reverse=True)

    # ---- Parte A: conteo de productos por categoría (CoRD vs VTEX-Oechsle) ----
    # usa la observación MÁS RECIENTE de cada categoría (acumulado entre corridas):
    # el barrido completo cubre todo el catálogo y las corridas parciales lo refrescan.
    count_rows = storage.latest_category_counts()
    max_count_run = max((c["run_id"] for c in count_rows), default=None)
    count_cats = []
    with_counts = matching = anomalies = 0
    total_cord = total_vtex = prod_missing = prod_extra = 0
    for c in count_rows:
        cc, vc = c["cord_count"], c["vtex_count"]
        if cc is None and vc is None:
            continue
        with_counts += 1
        cc0, vc0 = cc or 0, vc or 0
        total_cord += cc0
        total_vtex += vc0
        if vc0 > cc0:
            prod_missing += vc0 - cc0   # están en VTEX y no en CoRD
        elif cc0 > vc0:
            prod_extra += cc0 - vc0     # están en CoRD y no en VTEX
        is_match = cc0 == vc0
        if is_match:
            matching += 1
        if cc0 > vc0:
            anomalies += 1
        direction = "igual" if is_match else ("mas" if cc0 > vc0 else "menos")
        count_cats.append({
            "category_name": c["category_name"], "cord": cc, "vtex": vc,
            "diff": cc0 - vc0, "match": is_match, "anomaly": cc0 > vc0,
            "direction": direction,
            "cord_url": c["cord_url"], "vtex_url": c["vtex_url"],
        })
    # diferencias primero (por magnitud), iguales al final
    count_cats.sort(key=lambda x: (1 if x["match"] else 0, -abs(x["diff"])))
    faltan = with_counts - matching - anomalies
    pct = round(matching / with_counts * 100, 1) if with_counts else 0
    count_goal = goals.get("category_count")
    # delta: mismo acumulado pero sin la última corrida que aportó conteos
    prev_pct = None
    if max_count_run is not None:
        prows = storage.latest_category_counts(before_run_id=max_count_run)
        if prows:
            pm = sum(1 for x in prows if (x["cord_count"] or 0) == (x["vtex_count"] or 0))
            prev_pct = round(pm / len(prows) * 100, 1)
    count_summary = {
        "with_counts": with_counts, "matching": matching, "anomalies": anomalies,
        "faltan": faltan, "pct": pct,
        "total_cord": total_cord, "total_vtex": total_vtex,
        "prod_missing": prod_missing, "prod_extra": prod_extra,
        "prod_missing_pct": round(prod_missing / total_vtex * 100, 1) if total_vtex else 0,
        "goal": count_goal,
        "status": _status(pct, count_goal) if count_goal is not None else "ok",
        "gap": round(count_goal - pct, 1) if count_goal is not None and pct < count_goal else 0,
        "delta": round(pct - prev_pct, 1) if prev_pct is not None else None,
    }

    html = _env().get_template("index.html.j2").render(
        has_data=True,
        labels=labels,
        field_keys=list(labels.keys()),
        goals=goals,
        summary=summary,
        deltas=deltas,
        field_status=field_status,
        latest_id=latest_id,
        latest_file=history[0]["file"],
        priority_categories=cats[:25],
        worklists=worklists,
        count_summary=count_summary,
        count_categories=count_cats,
        history=history,
    )
    out = out_dir / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


def render_trends(cfg: Config, storage: Storage) -> Path:
    """Renderiza la página de tendencias a partir de todas las corridas finalizadas."""
    labels = Engine(cfg).comparator_labels()
    runs = storage.all_runs()
    points = []
    for r in runs:
        s = global_summary(storage, r["id"])
        points.append(
            {
                "run_id": r["id"],
                "started_at": r["started_at"],
                "avg_score": s.avg_score,
                "coverage_pct": s.coverage_pct,
                "products": s.products_compared,
                "field_ok": s.field_ok,
            }
        )
    html = _env().get_template("trends.html.j2").render(
        points=points,
        points_json=json.dumps(points, ensure_ascii=False),
        labels=labels,
        field_keys=list(labels.keys()),
    )
    out = cfg.path("paths.reports_dir") / "trends.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
