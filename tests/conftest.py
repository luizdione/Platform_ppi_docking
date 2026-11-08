# -*- coding: utf-8 -*-
"""Fixtures compartilhadas da suite (sem GPU, sem rede, sem haddock3).

O unico artefato pesado construido aqui e um `.cif` SINTETICO minimo com duas
cadeias proteicas em contato, usado por `test_interface_split.py` e
`test_haddock_adapter.py`. Ele e escrito com `MMCIFIO` (nunca com PDBIO
renomeado), justamente porque a armadilha que a suite guarda e o
`Bio.PDB.PDBParser` ler um `.cif` com ZERO atomos em silencio.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True, scope="session")
def _sem_aquecer_o_painel():
    """Impede o painel de pontuar de verdade durante a suite.

    A app `web/` poe os subprojetos para pontuar em segundo plano quando sobe.
    Num teste isso dispara SUBPROCESSOS sobre o dado real em disco (minutos de CPU)
    -- e chegou a acontecer: um teste que trocava `PPI_DOCK_CACHE_DIR` por um
    diretorio temporario fez o aquecimento achar o cache vazio e repontuar os 4
    subprojetos, competindo com a propria suite. Aqui isso fica desligado para
    a sessao inteira.
    """
    import os

    anterior = os.environ.get("PPI_WEB_NO_WARMUP")
    os.environ["PPI_WEB_NO_WARMUP"] = "1"
    yield
    if anterior is None:
        os.environ.pop("PPI_WEB_NO_WARMUP", None)
    else:
        os.environ["PPI_WEB_NO_WARMUP"] = anterior


def _residuo(resname: str, numero: int, base, indice_serial: int):
    """Monta um residuo com 4 atomos pesados (N, CA, C, O) ao redor de `base`."""
    from Bio.PDB.Atom import Atom
    from Bio.PDB.Residue import Residue

    res = Residue((" ", numero, " "), resname, "")
    deslocamentos = {
        "N": (0.0, 0.0, 0.0),
        "CA": (1.45, 0.0, 0.0),
        "C": (2.4, 1.0, 0.0),
        "O": (2.3, 2.2, 0.0),
    }
    serial = indice_serial
    for nome, (dx, dy, dz) in deslocamentos.items():
        coord = (base[0] + dx, base[1] + dy, base[2] + dz)
        res.add(
            Atom(
                nome,
                coord,
                20.0,          # b-factor
                1.0,           # ocupacao
                " ",           # altloc
                nome.ljust(4),  # fullname
                serial,
                element=nome[0],
            )
        )
        serial += 1
    return res, serial


def _estrutura_sintetica():
    """Estrutura com cadeia A (5 res) e cadeia B (4 res) em contato < 5 A."""
    from Bio.PDB.Chain import Chain
    from Bio.PDB.Model import Model
    from Bio.PDB.Structure import Structure

    est = Structure("sintetico")
    modelo = Model(0)
    est.add(modelo)

    serial = 1
    cadeia_a = Chain("A")
    for i in range(5):
        # residuos 10..14 empilhados no eixo Y
        res, serial = _residuo("ALA", 10 + i, (0.0, 4.0 * i, 0.0), serial)
        cadeia_a.add(res)
    modelo.add(cadeia_a)

    cadeia_b = Chain("B")
    for i in range(4):
        # residuos 30..33 a ~4 A da cadeia A no eixo Z -> ha contato real
        res, serial = _residuo("GLY", 30 + i, (0.0, 4.0 * i, 4.0), serial)
        cadeia_b.add(res)
    modelo.add(cadeia_b)
    return est


@pytest.fixture(scope="session")
def cif_sintetico(tmp_path_factory) -> Path:
    """Caminho de um `.cif` de duas cadeias proteicas, escrito por MMCIFIO."""
    from Bio.PDB.mmcifio import MMCIFIO

    destino = tmp_path_factory.mktemp("cif") / "complexo_sintetico.cif"
    io = MMCIFIO()
    io.set_structure(_estrutura_sintetica())
    io.save(str(destino))
    return destino
