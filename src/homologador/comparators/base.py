"""Interfaz de comparador + registry de auto-registro.

El motor (`engine.py`) itera sobre REGISTRY sin conocer qué comparadores existen.
Para agregar uno: subclasear Comparator y decorar con @register.
"""
from __future__ import annotations

from typing import Type

from ..config import Config
from ..models import FieldResult, Product

# registry global: key -> instancia de Comparator
REGISTRY: dict[str, "Comparator"] = {}


class Comparator:
    """Clase base. Cada comparador evalúa un aspecto (precio, nombre, ...)."""

    key: str = ""          # identificador único, ej. "price"
    label: str = ""        # nombre legible para el dashboard

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def compare(self, cord: Product, vtex: Product) -> FieldResult:
        raise NotImplementedError


def register(cls: Type[Comparator]) -> Type[Comparator]:
    """Decorator: instancia y registra el comparador en REGISTRY.

    La instancia real se crea al primer uso vía init_registry(cfg); aquí sólo se
    deja la clase anotada para registro diferido.
    """
    _REGISTERED_CLASSES.append(cls)
    return cls


_REGISTERED_CLASSES: list[Type[Comparator]] = []


def init_registry(cfg: Config) -> dict[str, "Comparator"]:
    """Instancia todos los comparadores registrados con la config dada."""
    REGISTRY.clear()
    for cls in _REGISTERED_CLASSES:
        inst = cls(cfg)
        REGISTRY[inst.key] = inst
    return REGISTRY
