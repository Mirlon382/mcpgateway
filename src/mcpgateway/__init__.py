"""Passerelle MCP du Service Statistique Public.

Agrège plusieurs serveurs MCP derrière un point d'entrée unique, avec :

* un registre YAML déclaratif et validé (:mod:`mcpgateway.registry`) ;
* un endpoint de santé et un suivi de joignabilité des amonts (:mod:`mcpgateway.health`,
  :mod:`mcpgateway.upstreams`) ;
* des métriques journalisées en JSON — latence, erreurs, appels d'outils
  (:mod:`mcpgateway.metrics`) ;
* des délais et des limites de concurrence (:mod:`mcpgateway.middleware`) ;
* un catalogue exposé en ressources MCP (:mod:`mcpgateway.catalog`).

Point d'entrée : ``mcpgateway.server:main`` (``uv run mcpgateway``).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
