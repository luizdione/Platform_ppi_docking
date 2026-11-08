# -*- coding: utf-8 -*-
"""Configuracao do guarda-chuva: campanhas, limiares dos gates e caminhos.

Os limiares do GATE 1 espelham (e estendem) `scoring_engine.config.SCORE`,
somando o criterio de curadoria manual que validou o funil: ipSAE minimo,
cobertura de subdominio e face canonica. O GATE 2 usa DockQ/iRMSD do
`caprieval` do HADDOCK3.

Os YAMLs vivem NESTA pasta (versionados no git). Os DADOS que eles apontam
vivem FORA do repositorio, na arvore de resultados do usuario (ver
``PPI_DOCK_DATA_ROOT`` no README) - nunca no git.

Uso::

    from config import load_config
    funil = load_config("funnel")               # -> dict
    sub   = load_config("subproject.demo01")    # -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

#: Pasta que contem os arquivos ``.yaml`` de configuracao (esta mesma).
CONFIG_DIR = Path(__file__).resolve().parent


def available_configs() -> "list[str]":
    """Devolve os nomes (sem extensao) de todos os YAML desta pasta, ordenados."""
    return sorted(p.stem for p in CONFIG_DIR.glob("*.yaml"))


def config_path(name: str) -> Path:
    """Resolve ``name`` para o caminho do YAML dentro de :data:`CONFIG_DIR`.

    Aceita ``"funnel"`` ou ``"funnel.yaml"``. Levanta ``FileNotFoundError``
    com a lista do que existe - nunca devolve um caminho inexistente em
    silencio.
    """
    stem = name[:-5] if name.endswith(".yaml") else name
    if "/" in stem or "\\" in stem or stem.startswith("."):
        raise ValueError(
            f"nome de configuracao invalido: {name!r} "
            "(esperado apenas o nome do arquivo, sem caminho)"
        )
    path = CONFIG_DIR / f"{stem}.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"configuracao {stem!r} nao encontrada em {CONFIG_DIR}. "
            f"Disponiveis: {', '.join(available_configs()) or '(nenhuma)'}"
        )
    return path


def load_config(name: str) -> "dict[str, Any]":
    """Carrega e devolve o YAML ``name`` como dicionario.

    Falha alto em qualquer anomalia (arquivo ausente, YAML mal formado,
    conteudo vazio ou que nao seja um mapeamento) - jamais devolve ``{}``
    fingindo sucesso.
    """
    path = config_path(name)
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        raise ValueError(f"configuracao vazia: {path}")
    if not isinstance(data, dict):
        raise TypeError(
            f"configuracao {path} deveria ser um mapeamento no topo, "
            f"veio {type(data).__name__}"
        )
    return data


__all__ = ["CONFIG_DIR", "available_configs", "config_path", "load_config"]
