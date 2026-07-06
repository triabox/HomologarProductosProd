"""Carga de configuración desde config.yaml con acceso por atributos anidados."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# Raíz del proyecto = dos niveles arriba de este archivo (src/homologador/config.py)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"


class Config:
    """Wrapper liviano sobre el dict de config con acceso por punto y rutas absolutas."""

    def __init__(self, data: dict[str, Any], root: Path):
        self._data = data
        self.root = root

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        return cls(data, p.resolve().parent)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str) -> Path:
        """Resuelve una ruta de `paths.*` relativa a la raíz del proyecto."""
        rel = self.get(dotted)
        if rel is None:
            raise KeyError(f"path no configurado: {dotted}")
        p = Path(rel)
        return p if p.is_absolute() else (self.root / p)
