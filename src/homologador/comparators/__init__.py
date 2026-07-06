"""Comparadores pluggables.

Importar este paquete registra automáticamente todos los comparadores incluidos.
Agregar un comparador nuevo = crear un módulo aquí con una clase decorada con
`@register`, sin tocar el núcleo.
"""
from . import attributes, description, name, price, variants  # noqa: F401  (efecto: registro)
from .base import REGISTRY, Comparator, register  # noqa: F401

__all__ = ["REGISTRY", "Comparator", "register"]
