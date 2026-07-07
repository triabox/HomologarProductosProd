"""Motor de comparación: corre todos los comparadores registrados y arma el resultado.

No conoce qué comparadores existen: itera sobre REGISTRY. El score de homologación
(0-100) es el promedio ponderado de los scores por campo según `score_weights`.
"""
from __future__ import annotations

from .comparators.base import init_registry
from .config import Config
from .models import Product, ProductComparison


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.registry = init_registry(cfg)
        self.weights = cfg.get("score_weights", {}) or {}

    def comparator_keys(self) -> list[str]:
        return list(self.registry.keys())

    def comparator_labels(self) -> dict[str, str]:
        return {k: c.label for k, c in self.registry.items()}

    def compare(
        self,
        sku: str,
        cord: Product,
        vtex: Product | None,
        cord_url: str | None = None,
    ) -> ProductComparison:
        comp = ProductComparison(
            sku=str(sku),
            category_name=cord.category_name,
            cord_url=cord_url or cord.url,
            vtex_url=vtex.url if vtex else None,
            vtex_found=vtex is not None,
        )
        if vtex is None:
            comp.score = 0.0
            return comp

        for key, comparator in self.registry.items():
            comp.fields.append(comparator.compare(cord, vtex))

        comp.score = self._weighted_score(comp)
        return comp

    def _weighted_score(self, comp: ProductComparison) -> float:
        total_w = 0.0
        acc = 0.0
        for fr in comp.fields:
            if fr.severity.value == "NO_APLICA":
                continue  # el campo no existe en este producto: no pondera
            w = self.weights.get(fr.field, 1.0)
            total_w += w
            acc += w * fr.score
        return round((acc / total_w) * 100.0, 2) if total_w else 0.0
