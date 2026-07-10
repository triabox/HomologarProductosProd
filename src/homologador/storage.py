"""Persistencia en SQLite: histórico de corridas, resultados y checkpoint/cursor.

Esquema:
- runs            : una fila por corrida (timestamp, parámetros, totales).
- product_results : una fila por producto comparado en una corrida (score, vtex_found).
- field_results   : una fila por (producto, comparador) con score/severidad/detalle.
- category_stats  : KPIs agregados por categoría y corrida.
- cursor          : avance entre corridas para reanudar (presupuesto de tiempo diario).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

from .models import ProductComparison

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    params TEXT,
    categories_total INTEGER DEFAULT 0,
    categories_done INTEGER DEFAULT 0,
    products_compared INTEGER DEFAULT 0,
    avg_score REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS product_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    category_name TEXT,
    vtex_found INTEGER NOT NULL,
    score REAL NOT NULL,
    cord_url TEXT,
    vtex_url TEXT,
    error TEXT,
    FOREIGN KEY(run_id) REFERENCES runs(id)
);
CREATE TABLE IF NOT EXISTS field_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    field TEXT NOT NULL,
    ok INTEGER NOT NULL,
    score REAL NOT NULL,
    severity TEXT NOT NULL,
    detail TEXT,
    cord_value TEXT,
    vtex_value TEXT
);
CREATE TABLE IF NOT EXISTS category_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    category_name TEXT NOT NULL,
    sampled INTEGER NOT NULL,
    vtex_found INTEGER NOT NULL,
    avg_score REAL NOT NULL,
    field_ok_json TEXT,
    cord_url TEXT,
    vtex_url TEXT
);
CREATE TABLE IF NOT EXISTS attribute_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    category_name TEXT,
    label TEXT NOT NULL,
    kind TEXT NOT NULL          -- 'missing' (falta en CoRD) | 'mismatch' (valor distinto)
);
CREATE INDEX IF NOT EXISTS idx_attrgap_run ON attribute_gaps(run_id);
CREATE TABLE IF NOT EXISTS attr_visibility (
    label TEXT PRIMARY KEY,      -- label normalizada
    orig_label TEXT,             -- último label original visto (para mostrar)
    visible INTEGER DEFAULT 0,   -- veces confirmado visible en el front de VTEX
    not_visible INTEGER DEFAULT 0, -- veces confirmado NO visible en el front de VTEX
    excluded INTEGER DEFAULT 0   -- 1 = descartado (no se valida más)
);
CREATE TABLE IF NOT EXISTS cursor (
    category_id TEXT PRIMARY KEY,
    category_name TEXT,
    last_run_id INTEGER,
    last_done_at TEXT,
    sample_offset INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_field_run ON field_results(run_id);
CREATE INDEX IF NOT EXISTS idx_prod_run ON product_results(run_id);
"""


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Migraciones livianas para bases creadas con esquemas anteriores."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(cursor)")}
        if "sample_offset" not in cols:
            self.conn.execute(
                "ALTER TABLE cursor ADD COLUMN sample_offset INTEGER DEFAULT 0"
            )
        cs_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(category_stats)")}
        for col in ("cord_url", "vtex_url"):
            if col not in cs_cols:
                self.conn.execute(f"ALTER TABLE category_stats ADD COLUMN {col} TEXT")
        for col in ("cord_count", "vtex_count"):
            if col not in cs_cols:
                self.conn.execute(f"ALTER TABLE category_stats ADD COLUMN {col} INTEGER")

    def close(self) -> None:
        self.conn.close()

    # -- runs --------------------------------------------------------------
    def start_run(self, started_at: str, params: dict[str, Any]) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, params) VALUES (?, ?)",
            (started_at, json.dumps(params, ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        finished_at: str,
        categories_total: int,
        categories_done: int,
        products_compared: int,
        avg_score: float,
    ) -> None:
        self.conn.execute(
            """UPDATE runs SET finished_at=?, categories_total=?, categories_done=?,
                   products_compared=?, avg_score=? WHERE id=?""",
            (finished_at, categories_total, categories_done, products_compared,
             avg_score, run_id),
        )
        self.conn.commit()

    # -- resultados --------------------------------------------------------
    def save_comparison(self, run_id: int, comp: ProductComparison) -> None:
        self.conn.execute(
            """INSERT INTO product_results
               (run_id, sku, category_name, vtex_found, score, cord_url, vtex_url, error)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, comp.sku, comp.category_name, int(comp.vtex_found),
             comp.score, comp.cord_url, comp.vtex_url, comp.error),
        )
        for fr in comp.fields:
            self.conn.execute(
                """INSERT INTO field_results
                   (run_id, sku, field, ok, score, severity, detail, cord_value, vtex_value)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (run_id, comp.sku, fr.field, int(fr.ok), fr.score, fr.severity.value,
                 fr.detail, fr.cord_value, fr.vtex_value),
            )
            for kind in ("missing", "mismatch"):
                for label in fr.extra.get(kind, []):
                    self.conn.execute(
                        """INSERT INTO attribute_gaps
                           (run_id, sku, category_name, label, kind) VALUES (?,?,?,?,?)""",
                        (run_id, comp.sku, comp.category_name, label, kind),
                    )

    def save_category_stats(
        self, run_id: int, category_name: str, sampled: int, vtex_found: int,
        avg_score: float, field_ok: dict[str, float],
        cord_url: str = "", vtex_url: str = "",
        cord_count: "int | None" = None, vtex_count: "int | None" = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO category_stats
               (run_id, category_name, sampled, vtex_found, avg_score, field_ok_json,
                cord_url, vtex_url, cord_count, vtex_count)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (run_id, category_name, sampled, vtex_found, avg_score,
             json.dumps(field_ok, ensure_ascii=False), cord_url, vtex_url,
             cord_count, vtex_count),
        )
        self.conn.commit()

    # -- cursor / reanudación ---------------------------------------------
    def mark_category_done(
        self, category_id: str, category_name: str, run_id: int, when: str,
        sample_offset: int = 0,
    ) -> None:
        self.conn.execute(
            """INSERT INTO cursor
                   (category_id, category_name, last_run_id, last_done_at, sample_offset)
               VALUES (?,?,?,?,?)
               ON CONFLICT(category_id) DO UPDATE SET
                   category_name=excluded.category_name,
                   last_run_id=excluded.last_run_id,
                   last_done_at=excluded.last_done_at,
                   sample_offset=excluded.sample_offset""",
            (category_id, category_name, run_id, when, sample_offset),
        )
        self.conn.commit()

    def done_category_ids(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT category_id FROM cursor WHERE last_done_at IS NOT NULL"
        ).fetchall()
        return {r["category_id"] for r in rows}

    def sample_offsets(self) -> dict[str, int]:
        """Offset de rotación de muestreo por categoría (para elegir productos distintos)."""
        return {
            r["category_id"]: (r["sample_offset"] or 0)
            for r in self.conn.execute(
                "SELECT category_id, sample_offset FROM cursor"
            )
        }

    def reset_cursor(self) -> None:
        """Marca todas las categorías como pendientes para un nuevo ciclo,
        conservando el offset de rotación para que el próximo ciclo elija otros productos."""
        self.conn.execute("UPDATE cursor SET last_done_at=NULL")
        self.conn.commit()

    # -- lectura para reportes --------------------------------------------
    def get_run(self, run_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def last_finished_run_id(self, before_id: Optional[int] = None) -> Optional[int]:
        if before_id is not None:
            row = self.conn.execute(
                "SELECT id FROM runs WHERE finished_at IS NOT NULL AND id<? "
                "ORDER BY id DESC LIMIT 1",
                (before_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT id FROM runs WHERE finished_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return int(row["id"]) if row else None

    def category_stats(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM category_stats WHERE run_id=? ORDER BY category_name",
            (run_id,),
        ).fetchall()

    def product_results(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM product_results WHERE run_id=? ORDER BY score ASC",
            (run_id,),
        ).fetchall()

    def field_results(self, run_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM field_results WHERE run_id=?", (run_id,)
        ).fetchall()

    def failing_items(self, run_id: int, field: str, limit: int = 50) -> list[sqlite3.Row]:
        """Productos que fallan en un campo, priorizados por menor score (peor primero)."""
        return self.conn.execute(
            """SELECT fr.sku, pr.category_name, fr.cord_value, fr.vtex_value,
                      fr.detail, fr.score, pr.cord_url, pr.vtex_url
               FROM field_results fr
               JOIN product_results pr ON pr.run_id=fr.run_id AND pr.sku=fr.sku
               WHERE fr.run_id=? AND fr.field=? AND fr.ok=0
               ORDER BY fr.score ASC
               LIMIT ?""",
            (run_id, field, limit),
        ).fetchall()

    def failing_count(self, run_id: int, field: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) n FROM field_results WHERE run_id=? AND field=? AND ok=0",
            (run_id, field),
        ).fetchone()["n"]

    # -- visibilidad aprendida de atributos (front de VTEX) --------------
    def get_excluded_labels(self) -> set[str]:
        """Labels normalizadas descartadas (no visibles en el front de VTEX)."""
        return {
            r["label"] for r in self.conn.execute(
                "SELECT label FROM attr_visibility WHERE excluded=1"
            )
        }

    def get_decided_labels(self) -> set[str]:
        """Labels ya resueltas (descartadas o confirmadas visibles) — no re-verificar."""
        return {
            r["label"] for r in self.conn.execute(
                "SELECT label FROM attr_visibility WHERE excluded=1 OR visible>=1"
            )
        }

    def record_visibility(
        self, label: str, orig_label: str, is_visible: bool, threshold: int = 2
    ) -> bool:
        """Registra una observación de visibilidad. Devuelve True si acaba de descartarse."""
        col = "visible" if is_visible else "not_visible"
        self.conn.execute(
            f"""INSERT INTO attr_visibility(label, orig_label, {col})
                VALUES (?,?,1)
                ON CONFLICT(label) DO UPDATE SET
                    orig_label=excluded.orig_label,
                    {col}={col}+1""",
            (label, orig_label),
        )
        row = self.conn.execute(
            "SELECT visible, not_visible, excluded FROM attr_visibility WHERE label=?",
            (label,),
        ).fetchone()
        newly = False
        # descartar solo si nunca se vio visible y ya hay >=threshold confirmaciones de oculto
        if not row["excluded"] and row["visible"] == 0 and row["not_visible"] >= threshold:
            self.conn.execute(
                "UPDATE attr_visibility SET excluded=1 WHERE label=?", (label,)
            )
            newly = True
        self.conn.commit()
        return newly

    def attribute_gaps(self, run_id: int, kind: str = "missing") -> list[sqlite3.Row]:
        """Agregado de atributos por label: cuántos productos y categorías afectados."""
        return self.conn.execute(
            """SELECT label,
                      COUNT(DISTINCT sku) AS products,
                      COUNT(DISTINCT category_name) AS categories,
                      GROUP_CONCAT(DISTINCT sku) AS skus
               FROM attribute_gaps
               WHERE run_id=? AND kind=?
               GROUP BY label
               ORDER BY products DESC, label ASC""",
            (run_id, kind),
        ).fetchall()

    def dangling_run_ids(self) -> list[int]:
        """IDs de corridas que quedaron sin finalizar (proceso cortado a mitad)."""
        return [
            int(r["id"]) for r in self.conn.execute(
                "SELECT id FROM runs WHERE finished_at IS NULL ORDER BY id"
            )
        ]

    def latest_category_counts(self, before_run_id: Optional[int] = None) -> list[sqlite3.Row]:
        """Conteo más reciente por categoría, acumulado a través de TODAS las corridas.

        Un barrido completo puebla todo el catálogo y las corridas parciales solo
        actualizan las categorías que tocan. `before_run_id` limita a corridas
        anteriores (para calcular el delta del indicador).
        """
        cond = "AND run_id<?" if before_run_id is not None else ""
        params = (before_run_id,) if before_run_id is not None else ()
        # solo observaciones COMPLETAS (ambos conteos): una lectura fallida de CoRD
        # (cord_count NULL, ej. corridas durante caídas) no pisa a una medición real
        return self.conn.execute(
            f"""SELECT cs.* FROM category_stats cs
                JOIN (SELECT category_name, MAX(run_id) mr FROM category_stats
                      WHERE cord_count IS NOT NULL AND vtex_count IS NOT NULL {cond}
                      GROUP BY category_name) t
                  ON t.category_name=cs.category_name AND t.mr=cs.run_id
                WHERE cs.cord_count IS NOT NULL AND cs.vtex_count IS NOT NULL""",
            params,
        ).fetchall()

    def all_runs(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id ASC"
        ).fetchall()
