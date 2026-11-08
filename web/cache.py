# -*- coding: utf-8 -*-
"""Cache em DISCO do manifesto de pares, com calculo em segundo plano.

Por que existe: pontuar um subprojeto ao vivo e caro de verdade. Uma campanha
tem tipicamente dezenas a centenas de runs, e CADA um passa por
`scoring_engine.score.score_case` (que calcula BSA por SASA) e pelo parser de
interface -- da minutos, nao milissegundos. Isso nao pode acontecer dentro de
uma requisicao HTTP, nem pode ser refeito a cada restart do servidor.

Como resolve:

* o manifesto pronto vai para DISCO, com chave = `fingerprint_runs` (n de runs +
  mtime mais novo). Enquanto o dado nao muda, a leitura e instantanea; quando
  muda, a chave muda sozinha e o manifesto e refeito;
* o calculo roda em UMA thread por subprojeto (`_EM_CURSO`), entao o aquecimento
  da subida e um acesso simultaneo nunca calculam a mesma coisa duas vezes -- o
  que antes saturava a CPU e deixava o servidor irresponsivo;
* a pagina NUNCA espera: pergunta `estado()`, e se ainda nao esta pronto mostra
  "pontuando..." e volta a perguntar.

O arquivo fica FORA do repo (dado derivado), em `~/.ppi_docking_cache/` ou onde
`PPI_DOCK_CACHE_DIR` apontar.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict

_MEM: "Dict[str, tuple]" = {}
_EM_CURSO: "Dict[str, Dict[str, Any]]" = {}
_LOCK = threading.Lock()
_SEGURO = re.compile(r"[^A-Za-z0-9_.-]")

PRONTO = "pronto"
CALCULANDO = "calculando"
AUSENTE = "ausente"
FALHOU = "falhou"


def cache_dir() -> Path:
    bruto = os.environ.get("PPI_DOCK_CACHE_DIR")
    destino = Path(bruto) if bruto else (Path.home() / ".ppi_docking_cache")
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def _arquivo(key: str, impressao: str) -> Path:
    nome = _SEGURO.sub("_", f"manifesto_{key}_{impressao}") + ".json"
    return cache_dir() / nome


def _do_disco(key: str, impressao: str) -> "Dict[str, Any] | None":
    caminho = _arquivo(key, impressao)
    if not caminho.is_file():
        return None
    try:
        manifesto = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        caminho.unlink(missing_ok=True)  # corrompido: refaz
        return None
    with _LOCK:
        _MEM[key] = (impressao, manifesto)
    return manifesto


def pronto(key: str, impressao: "str | None") -> "Dict[str, Any] | None":
    """Manifesto ja disponivel (memoria ou disco), ou ``None``. Nunca calcula."""
    if impressao is None:
        return None
    guardado = _MEM.get(key)
    if guardado and guardado[0] == impressao:
        return guardado[1]
    return _do_disco(key, impressao)


def estado(key: str, impressao: "str | None") -> "Dict[str, Any]":
    """Situacao do manifesto: pronto / calculando (com quanto tempo) / ausente."""
    if pronto(key, impressao) is not None:
        return {"estado": PRONTO}
    curso = _EM_CURSO.get(key)
    if curso and curso.get("viva"):
        return {
            "estado": CALCULANDO,
            "desde_s": round(time.monotonic() - curso["inicio"], 1),
        }
    if curso and curso.get("erro"):
        return {"estado": FALHOU, "erro": curso["erro"]}
    return {"estado": AUSENTE}


def garantir(key: str, impressao: "str | None") -> "Dict[str, Any]":
    """Assegura que o manifesto esteja sendo calculado, SEM bloquear.

    Devolve o mesmo dict de ``estado()``. Se ja ha um calculo em curso para este
    subprojeto, nao dispara outro -- e o que impede o aquecimento da subida e um
    acesso do usuario de duplicarem trabalho (e saturarem a maquina).
    """
    if impressao is None:
        return {"estado": AUSENTE, "erro": "sem fingerprint (raiz inacessivel?)"}
    if pronto(key, impressao) is not None:
        return {"estado": PRONTO}

    with _LOCK:
        curso = _EM_CURSO.get(key)
        if curso and curso.get("viva"):
            return {
                "estado": CALCULANDO,
                "desde_s": round(time.monotonic() - curso["inicio"], 1),
            }
        registro: "Dict[str, Any]" = {
            "viva": True,
            "inicio": time.monotonic(),
            "erro": "",
        }
        _EM_CURSO[key] = registro

    caminho = _arquivo(key, impressao)

    def _tarefa() -> None:
        """Vigia o SUBPROCESSO que pontua (ver `web/scorer.py`).

        A conta roda fora deste processo de proposito: em thread, o GIL do
        trabalho de CPU deixava o servidor engasgado (pagina ja cacheada levando
        >30 s). Aqui a thread so espera o processo e le o arquivo pronto.
        """
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "web.scorer", key, str(caminho)],
                cwd=str(Path(__file__).resolve().parent.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            registro["pid"] = proc.pid
            saida, _ = proc.communicate()
            if proc.returncode != 0:
                registro["erro"] = (
                    f"pontuacao falhou (rc={proc.returncode}): "
                    f"{(saida or '').strip()[-400:]}"
                )
                return
            manifesto = json.loads(caminho.read_text(encoding="utf-8"))
            with _LOCK:
                _MEM[key] = (impressao, manifesto)
            # aposenta manifestos antigos deste subprojeto (outra impressao)
            for velho in cache_dir().glob(f"manifesto_{_SEGURO.sub('_', key)}_*.json"):
                if velho != caminho:
                    velho.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - o estado guarda o motivo
            registro["erro"] = f"{type(exc).__name__}: {exc}"
        finally:
            registro["viva"] = False

    threading.Thread(target=_tarefa, name=f"manifesto-{key}", daemon=True).start()
    return {"estado": CALCULANDO, "desde_s": 0.0}
