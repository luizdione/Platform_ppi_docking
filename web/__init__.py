# -*- coding: utf-8 -*-
"""Dashboard da plataforma (FastAPI, porta 8777).

UMA app serve as duas metades do painel:

* a **interface** (HTML/CSS/JS lendo `web/static/ppi.css` -- a fonte unica
  de estilo);
* a **API de execucao** (`/api/...`), que enfileira e acompanha os jobs
  pesados (funil, HADDOCK3, MD/MM-GBSA).

Substitui o Streamlit `gui/` que ocupava esta porta e absorve a antiga ponte
FastAPI da 8778.

Subir: ``python -m web``  ->  http://localhost:8777
"""

from __future__ import annotations

__version__ = "0.1.0"
PORTA_PADRAO = 8777

__all__ = ["__version__", "PORTA_PADRAO"]
