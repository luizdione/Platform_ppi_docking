# -*- coding: utf-8 -*-
"""Ponte OPCIONAL para um modulo `dashboard_core` externo.

Algumas paginas do painel (diagnostico de ambiente, receitas de experimento,
descoberta de MD, figuras de trajetoria) sao servidas por um modulo externo
chamado `dashboard_core`, que NAO faz parte deste repositorio: ele pertence a
camada de predicao/analise, a montante e a jusante do funil. Em vez de
reimplementa-lo aqui (o que criaria uma segunda verdade que envelhece sozinha),
este modulo o IMPORTA quando o usuario aponta onde ele esta.

Onde procurar: a env ``PPI_ENGINE_APP_DIR`` (pasta que contem
`dashboard_core.py`). A pasta e inserida no `sys.path`, porque tipicamente nao
e um pacote instalavel. SEM essa env -- o caso normal de quem so usa o funil --
as paginas que dependem dela mostram o motivo em vez de quebrar; o funil, a
pontuacao, os portoes e o HADDOCK3 nao a usam para nada.
"""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

#: env que aponta a pasta com `dashboard_core.py` (sem default de disco)
ENGINE_APP_DIR_ENV = "PPI_ENGINE_APP_DIR"


def _app_do_motor() -> "Path | None":
    """Pasta do `dashboard_core` externo, ou None se nao declarada."""
    bruto = os.environ.get(ENGINE_APP_DIR_ENV, "").strip()
    return Path(bruto) if bruto else None


class MotorIndisponivel(RuntimeError):
    """O `dashboard_core` do repo do motor nao pode ser carregado."""


@lru_cache(maxsize=1)
def core():
    """Modulo `dashboard_core` carregado (uma vez por processo)."""
    pasta = _app_do_motor()
    if pasta is None:
        raise MotorIndisponivel(
            f"{ENGINE_APP_DIR_ENV} nao definida. Esta pagina depende de um "
            "modulo `dashboard_core` externo (camada de predicao/analise), que "
            "nao faz parte deste repositorio. O funil nao precisa dele."
        )
    if not (pasta / "dashboard_core.py").is_file():
        raise MotorIndisponivel(
            f"nao achei dashboard_core.py em {pasta} (veja {ENGINE_APP_DIR_ENV})"
        )
    if str(pasta) not in sys.path:
        sys.path.insert(0, str(pasta))
    try:
        import dashboard_core  # noqa: WPS433
    except Exception as exc:  # noqa: BLE001
        raise MotorIndisponivel(f"{type(exc).__name__}: {exc}") from exc
    return dashboard_core


def disponivel() -> "Dict[str, Any]":
    """Diz se a ponte esta de pe (entra no /health e nas paginas)."""
    pasta = _app_do_motor()
    try:
        modulo = core()
    except MotorIndisponivel as exc:
        return {"ok": False, "erro": str(exc), "raiz": str(pasta or "")}
    return {"ok": True, "raiz": str(pasta or ""), "arquivo": modulo.__file__}


# ---------------------------------------------------------------------------
# leituras usadas pelas paginas
# ---------------------------------------------------------------------------
#: Raiz do dado. As pastas de entrada/saida do diagnostico saem daqui (ou das
#: envs mais especificas), porque `health_report` precisa de caminhos
#: concretos -- passar None quebra. Sem default de disco: na falta das envs,
#: usa o diretorio corrente.


def _raiz_dado() -> Path:
    bruto = (os.environ.get("PPI_DOCK_DATA_ROOT", "").strip()
             or os.environ.get("PPI_DATA_ROOT", "").strip())
    return Path(bruto) if bruto else Path.cwd()


def diagnostico(inputs_dir: str = "", outputs_dir: str = "") -> "Dict[str, Any]":
    """Relatorio de sanidade do ambiente (dependencias, ferramentas, pastas)."""
    modulo = core()
    raiz = _raiz_dado()
    entradas = inputs_dir or os.environ.get("PPI_INPUTS_DIR") or str(raiz)
    saidas = outputs_dir or os.environ.get("PPI_OUTPUTS_DIR") or str(raiz)
    return modulo.health_report(Path(entradas), Path(saidas))


def gpu() -> "Dict[str, Any]":
    modulo = core()
    disponivel_gpu, descricao = modulo.gpu_status()
    return {"disponivel": bool(disponivel_gpu), "descricao": descricao}


def runs_de(raizes: "List[str]") -> "List[Dict[str, Any]]":
    """Predicoes (`boltz_results_*`) das raizes de um subprojeto."""
    modulo = core()
    achados = modulo.discover_runs_in([Path(r) for r in raizes])
    return [
        {"nome": Path(str(r)).name, "caminho": str(r)}
        for r in (achados or [])
    ]


def mds_de(raizes: "List[str]") -> "List[Dict[str, Any]]":
    """Rodadas de MD encontradas nas raizes de um subprojeto."""
    modulo = core()
    achados = modulo.discover_md_runs_in([Path(r) for r in raizes]) or []
    saida: "List[Dict[str, Any]]" = []
    for item in achados:
        if isinstance(item, dict):
            saida.append(
                {
                    "nome": Path(str(item.get("dir") or item.get("path") or "")).name,
                    "caminho": str(item.get("dir") or item.get("path") or ""),
                    "tem_dcd": bool(item.get("has_dcd")),
                    "tem_traj": bool(item.get("has_traj")),
                    "tem_energia": bool(item.get("has_energy")),
                }
            )
        else:
            saida.append({"nome": Path(str(item)).name, "caminho": str(item)})
    return saida


def receitas() -> "Dict[str, Any]":
    """Receitas de experimento declaradas no projeto-alvo (`PPI_TARGET_DIR`)."""
    modulo = core()
    alvo = modulo.target_dir()
    if not alvo:
        return {"alvo": "", "receitas": []}
    achadas = modulo.list_recipes(alvo) or []
    return {
        "alvo": str(alvo),
        "receitas": [
            {"nome": Path(str(r)).name, "caminho": str(r)} for r in achadas
        ],
    }
