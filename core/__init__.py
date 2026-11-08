# -*- coding: utf-8 -*-
"""Nucleo de dados da plataforma - logica testavel, SEM camada de UI.

``core.data`` reune a leitura de subprojetos/predicoes, a pontuacao ao vivo
(``scoring_engine.score``), as figuras (Plotly) e os helpers de estrutura. Nasceu como
``gui/gui_core.py`` (Streamlit) e foi promovido a nucleo neutro quando o
dashboard virou uma app FastAPI (``web/``): quem consome os dados nao deve
depender de nenhum framework de interface.
"""

from __future__ import annotations

__all__ = ["data"]
