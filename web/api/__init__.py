# -*- coding: utf-8 -*-
"""Rotas da API do painel (`/api/...`).

Cada modulo daqui expoe um `router` (APIRouter) que a app monta. As rotas
herdadas da ponte antiga continuam servidas pela propria sub-app absorvida em
`/api`; o que nasce no painel -- fila de jobs, execucao das acoes, publicacao --
mora aqui.
"""

from __future__ import annotations

__all__ = ["jobs"]
