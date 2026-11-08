# -*- coding: utf-8 -*-
"""Rotas do autoteste: dispara, acompanha e devolve o ultimo resultado.

O autoteste roda subprocessos (`--help` de cada acao, ciclo de job real), entao
leva dezenas de segundos -- longe demais para prender uma requisicao HTTP. Aqui
ele roda numa thread e a pagina pergunta o estado ate ficar pronto, o mesmo
padrao ja usado na pontuacao dos subprojetos.

O ultimo resultado vai para disco: depois de fechar o navegador (ou reiniciar o
servidor) ainda da para ver o que foi testado e quando.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter

router = APIRouter()

_ESTADO: "Dict[str, Any]" = {"rodando": False, "inicio": 0.0}
_LOCK = threading.Lock()


def _arquivo() -> Path:
    from web.jobs import jobs_dir

    return jobs_dir() / "autoteste.json"


def ultimo() -> "Dict[str, Any] | None":
    """Ultimo resultado gravado, ou None se nunca rodou."""
    alvo = _arquivo()
    if not alvo.is_file():
        return None
    try:
        return json.loads(alvo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def estado() -> "Dict[str, Any]":
    """Situacao atual: rodando (ha quanto tempo) + o ultimo resultado."""
    with _LOCK:
        rodando = bool(_ESTADO.get("rodando"))
        desde = round(time.monotonic() - _ESTADO["inicio"], 1) if rodando else None
    return {"rodando": rodando, "desde_s": desde, "resultado": ultimo()}


def disparar() -> "Dict[str, Any]":
    """Poe o autoteste para rodar (sem bloquear). Nao dispara dois ao mesmo tempo."""
    with _LOCK:
        if _ESTADO.get("rodando"):
            return {
                "rodando": True,
                "desde_s": round(time.monotonic() - _ESTADO["inicio"], 1),
                "ja_estava": True,
            }
        _ESTADO["rodando"] = True
        _ESTADO["inicio"] = time.monotonic()

    def _tarefa() -> None:
        try:
            from web import autoteste

            resultado = autoteste.rodar()
            resultado["quando"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                _arquivo().write_text(
                    json.dumps(resultado, ensure_ascii=False, indent=1),
                    encoding="utf-8",
                )
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001 - o estado guarda o motivo
            try:
                _arquivo().write_text(
                    json.dumps(
                        {
                            "erro": f"{type(exc).__name__}: {exc}",
                            "quando": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "blocos": [], "total": 0, "passou": 0, "falhou": 0,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
        finally:
            with _LOCK:
                _ESTADO["rodando"] = False

    threading.Thread(target=_tarefa, name="autoteste", daemon=True).start()
    return {"rodando": True, "desde_s": 0.0, "ja_estava": False}


@router.get("/autoteste")
def ver_autoteste() -> "Dict[str, Any]":
    return estado()


@router.post("/autoteste")
def rodar_autoteste() -> "Dict[str, Any]":
    return disparar()
