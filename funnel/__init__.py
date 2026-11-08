# -*- coding: utf-8 -*-
"""Funil do guarda-chuva: Boltz-2 -> score -> GATE 1 -> HADDOCK3 -> GATE 2.

Camada FINA: nao reimplementa motor nenhum. A pontuacao vem do subpacote
`scoring_engine` (vendorizado neste repositorio) e o docking vem de
`engines.haddock3`, do repositorio de docking (ver README). Aqui moram apenas
o contrato de dados (`contract`), os portoes e a orquestracao.

Modulos existentes: `contract` (formato), `gate` (os dois portoes), `scoring`
(monta o `PairManifest` do que existe em disco), `interface` (residuos de
contato -> AIRs do HADDOCK), `split` (complexo .cif -> receptor.pdb +
ligand.pdb), `haddock_adapter` (ponte para o `Haddock3Engine`) e `pipeline`
(encadeia tudo para uma campanha inteira).

So `contract` e `gate` sao reexportados aqui: os demais dependem de
biopython/numpy (via `scoring_engine`), de pyyaml ou do repositorio dos
engines, enquanto estes dois precisam continuar importaveis num ambiente
leve. Use
`from funnel import interface, split, scoring, pipeline`.
"""

from __future__ import annotations

from .contract import (  # noqa: F401
    STAGES,
    GateResult,
    HaddockArtifacts,
    PairManifest,
    Scores,
    load_manifest,
    save_manifest,
)
from .gate import (  # noqa: F401
    DEFAULT_CRITERIA,
    classify_with_ppi,
    gate_boltz,
    gate_haddock,
    load_criteria,
)

__version__ = "0.1.0"

__all__ = [
    "STAGES",
    "GateResult",
    "HaddockArtifacts",
    "PairManifest",
    "Scores",
    "load_manifest",
    "save_manifest",
    "DEFAULT_CRITERIA",
    "load_criteria",
    "gate_boltz",
    "gate_haddock",
    "classify_with_ppi",
    "__version__",
]
