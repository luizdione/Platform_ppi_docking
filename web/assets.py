# -*- coding: utf-8 -*-
"""Garante as bibliotecas de terceiros do painel (3Dmol.js e plotly.js).

As duas sao pesadas (~530 KB e ~4,7 MB) e NAO sao versionadas: este modulo as
recria a partir do proprio pacote python `plotly` (que ja traz o bundle) e,
para o 3Dmol, de um CDN publico -- uma vez, no primeiro arranque. Depois disso
o painel funciona OFFLINE, sem inchar o repositorio.

Chamado uma vez, na subida da app. Nunca derruba a app: se faltar uma lib, a
pagina degrada com aviso visivel em vez de quebrar em silencio.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

STATIC = Path(__file__).resolve().parent / "static"

#: copia local ja baixada, se o usuario apontar uma (env PPI_3DMOL_JS).
_3DMOL_LOCAL_ENV = "PPI_3DMOL_JS"
#: fontes publicas do bundle do 3Dmol.js (upstream e CDN).
_3DMOL_URLS = [
    "https://3dmol.org/build/3Dmol-min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/3Dmol/2.4.2/3Dmol-min.js",
]


def _garante_plotly() -> "Dict[str, Any]":
    destino = STATIC / "plotly.min.js"
    if destino.is_file() and destino.stat().st_size > 1_000_000:
        return {"lib": "plotly", "ok": True, "origem": "cache local"}
    try:
        import plotly  # noqa: WPS433

        fonte = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
        if not fonte.is_file():
            return {"lib": "plotly", "ok": False, "erro": f"nao achei {fonte}"}
        shutil.copyfile(fonte, destino)
        return {"lib": "plotly", "ok": True, "origem": str(fonte)}
    except Exception as exc:  # noqa: BLE001
        return {"lib": "plotly", "ok": False, "erro": f"{type(exc).__name__}: {exc}"}


def _garante_3dmol() -> "Dict[str, Any]":
    destino = STATIC / "3Dmol-min.js"
    if destino.is_file() and destino.stat().st_size > 200_000:
        return {"lib": "3Dmol", "ok": True, "origem": "cache local"}
    local = os.environ.get(_3DMOL_LOCAL_ENV, "").strip()
    if local and Path(local).is_file():
        shutil.copyfile(local, destino)
        return {"lib": "3Dmol", "ok": True, "origem": local}
    for url in _3DMOL_URLS:  # ultimo recurso: baixar do CDN publico
        try:
            subprocess.run(
                ["curl", "-fsSL", "-o", str(destino), url], check=True, timeout=120
            )
            if destino.is_file() and destino.stat().st_size > 200_000:
                return {"lib": "3Dmol", "ok": True, "origem": url}
        except Exception:  # noqa: BLE001 - tenta a proxima URL
            continue
    return {"lib": "3Dmol", "ok": False, "erro": "bundle ausente e download falhou"}


def ensure_assets() -> "List[Dict[str, Any]]":
    """Garante as libs em `web/static/`; devolve o relatorio (entra no /health)."""
    STATIC.mkdir(parents=True, exist_ok=True)
    return [_garante_3dmol(), _garante_plotly()]
