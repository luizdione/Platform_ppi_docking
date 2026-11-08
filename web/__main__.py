# -*- coding: utf-8 -*-
"""Sobe o dashboard: ``python -m web`` -> http://localhost:8777.

Host/porta saem das env vars ``PPI_WEB_HOST``/``PPI_WEB_PORT``. O default de
host e ``127.0.0.1`` (so a propria maquina): este painel EXECUTA jobs, entao
nao deve ficar exposto na rede sem que o autor peca explicitamente.
"""

from __future__ import annotations

import os

import uvicorn

from web import PORTA_PADRAO


def main() -> int:
    host = os.environ.get("PPI_WEB_HOST", "127.0.0.1")
    porta = int(os.environ.get("PPI_WEB_PORT", PORTA_PADRAO))
    uvicorn.run("web.app:app", host=host, port=porta, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
