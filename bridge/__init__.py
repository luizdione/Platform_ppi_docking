# -*- coding: utf-8 -*-
"""Ponte HTTP (FastAPI) que expoe o funil ao n8n.

O no *Execute Command* do n8n roda DENTRO do container e por isso nao alcanca a
GPU nem os ambientes conda do host. A ponte roda no HOST e o
container fala com ela por ``http://host.docker.internal:<porta>``.

Modulos:

* ``bridge.runners`` - subprocessos (CLI externo, haddock3) e registro de jobs;
* ``bridge.app``     - a aplicacao FastAPI com as rotas.

Subir o servico (montado em /api do painel por padrao; antes era um servico
separado na 8778)::

    python -m bridge

O import de ``app`` e PREGUICOSO para que `import bridge.runners` (usado em
testes) nao exija fastapi/uvicorn instalados.
"""

from __future__ import annotations

from typing import Any

__all__ = ["app", "porta_padrao", "runners", "__version__"]

__version__ = "0.1.0"


def __getattr__(nome: str) -> Any:
    """Resolve ``bridge.app``/``bridge.porta_padrao`` sob demanda."""
    if nome in {"app", "porta_padrao"}:
        from . import app as _app

        return getattr(_app, nome)
    raise AttributeError(f"module 'bridge' has no attribute {nome!r}")


if __name__ == "__main__":  # pragma: no cover - execucao manual do servico
    import os

    import uvicorn

    from .app import porta_padrao as _porta

    uvicorn.run(
        "bridge.app:app",
        host=os.environ.get("PPI_DOCK_BRIDGE_HOST", "0.0.0.0"),
        port=_porta(),
        reload=False,
    )
