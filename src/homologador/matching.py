"""Muestreo determinístico de productos por categoría.

Se toman hasta `sampling.per_category` productos de cada categoría. El muestreo es
determinístico (ordena por un hash estable de sku+seed) para que distintas corridas
elijan la misma muestra y los KPIs sean comparables en el tiempo.
"""
from __future__ import annotations

import hashlib

from .config import Config
from .models import DiscoveredProduct


def _stable_key(sku: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{sku}".encode()).hexdigest()


class Sampler:
    def __init__(self, cfg: Config):
        self.n = cfg.get("sampling.per_category", 20)
        self.seed = cfg.get("sampling.seed", 42)
        self.rotate = cfg.get("sampling.rotate", False)

    def sample(
        self, products: list[DiscoveredProduct], offset: int = 0
    ) -> list[DiscoveredProduct]:
        """Devuelve hasta `n` productos. Con `offset` toma una ventana rotatoria
        (cíclica) sobre el orden base, para validar distintos productos en cada corrida."""
        ordered = sorted(products, key=lambda p: _stable_key(p.sku, self.seed))
        total = len(ordered)
        if total <= self.n or offset % total == 0:
            return ordered[: self.n]
        start = offset % total
        return [ordered[(start + i) % total] for i in range(self.n)]

    def next_offset(self, current: int, total: int) -> int:
        """Avanza el offset de rotación para la próxima corrida de esa categoría."""
        if not self.rotate or total <= self.n:
            return 0
        return (current + self.n) % total
