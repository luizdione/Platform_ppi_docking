# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# PROVENANCE / PROCEDENCIA
#
# This module was copied verbatim (apart from the import paths, this header and
# the sanitisation of one internal comment) from the author's own MIT-licensed
# package `ppi` (ppi_platform), by Luiz Dione Barbosa De-Melo (IFRJ).
# Only the modules the funnel actually imports were vendored here, so that this
# repository runs without the private package installed. See README, section
# "Vendored scoring engine".
# ---------------------------------------------------------------------------
"""
scoring_engine._common - utilitarios internos compartilhados pela camada de ANALISE
(estagios 4-5 e auxiliares: score, rank, structure). Consolida helpers que antes
estavam duplicados entre modulos e reimplementados em scripts por-projeto:

  - load_npz        : carrega a 1a array de um .npz do Boltz (pae/pde/plddt)
  - plddt_to_100    : normaliza pLDDT para a escala 0-100 (Boltz emite 0-1)
  - parse_model     : parseia um .cif e devolve o 1o modelo (biopython)
  - chain_ids_sizes : (ids, tamanhos) de residuos padrao por cadeia
  - interface_pairs : busca UNICA de interface inter-cadeia (NeighborSearch)

Dependencias: numpy (topo) + biopython (import lazy dentro das funcoes). Este
modulo pertence a camada de analise; **nunca** importa-lo a partir do core
stdlib (estagios 1-2: `sequences`, `inputs`), que deve permanecer sem numpy.
"""
from __future__ import annotations
import numpy as np


def load_npz(path):
    """Carrega a 1a array de um .npz (Boltz grava pae/pde/plddt com uma array).

    Usa a 1a chave POSICIONALMENTE (robusto ao nome real: "pae"/"pde"/"plddt")
    e faz `squeeze` do eventual eixo de batch (padrao validado em uso real).
    """
    d = np.load(path)
    return np.squeeze(d[d.files[0]])


def plddt_to_100(arr):
    """Normaliza pLDDT para a escala 0-100.

    O Boltz emite pLDDT FRACIONARIO (0-1); a convencao publicavel (e a do
    AlphaFold) e 0-100. Multiplica por 100 apenas quando os valores vierem em
    0-1, de modo idempotente (nao mexe se ja estiver em 0-100).
    """
    arr = np.asarray(arr, dtype=float)
    return arr * 100.0 if arr.size and float(arr.max()) <= 1.0 else arr


def parse_model(cif):
    """1o modelo de um .cif (MMCIFParser silencioso). Import lazy de biopython."""
    from Bio.PDB.MMCIFParser import MMCIFParser
    return next(MMCIFParser(QUIET=True).get_structure("x", str(cif)).get_models())


def chain_ids_sizes(model_or_cif):
    """(ids, sizes) de cada cadeia, contando so residuos padrao (hetero/agua fora).

    Aceita um modelo biopython ja parseado OU um caminho de .cif. Ordem = ordem
    das cadeias no modelo (para o Boltz, A, B, C... = ordem das sequencias no YAML).
    """
    model = model_or_cif if hasattr(model_or_cif, "get_chains") else parse_model(model_or_cif)
    ids, sizes = [], []
    for c in model.get_chains():
        n = sum(1 for r in c if r.id[0] == " ")
        if n:
            ids.append(c.id)
            sizes.append(n)
    return ids, sizes


def interface_pairs(model, cutoff: float = 5.0, protein_only: bool = False):
    """Busca UNICA de interface inter-cadeia (atomos pesados a < cutoff A).

    Base compartilhada por `score._interface`, `structure.complex_interface` e
    `structure.oligomer_interface` (antes, tres copias quase identicas com
    contagens de contato divergentes).

    `protein_only` (default False, IDENTICO ao comportamento historico): quando
    True, exclui residuos HETERO (`residue.id[0] != " "` -- ligantes/cofatores como
    ATP/MG, tipicamente gravados pelo Boltz como cadeia(s) hetero propria(s)) da
    busca de vizinhanca. Sem isso, um residuo de proteina que so toca o ligante
    (ex. bolso de nucleotideo) entra na interface reportada como se fosse contato
    proteina-proteina -- usado por `structure.complex_interface` (interface
    isca-parceiro EM PROTEINA). Default False preserva o comportamento de
    `score`/`af3`/`validate`/`oligomer_interface`, que ja chamam esta funcao sem
    esse parametro.

    Retorna (chains, per, contacts):
      chains   : lista de cadeias do modelo
      per      : {chain_id: set(resnum)} residuos de interface por cadeia
      contacts : set de pares de residuos inter-cadeia, cada um como a tupla
                 ORDENADA ((c, resnum, resname), (c, resnum, resname)). A
                 ordenacao canoniza a direcao do par -> cada par residuo-residuo
                 conta UMA vez (`len(contacts)` = n. de contatos residuo-residuo,
                 sem a dupla contagem que havia em `score._interface`).
    """
    from Bio.PDB import NeighborSearch
    chains = [c for c in model]
    if len(chains) < 2:
        return chains, {}, set()
    if protein_only:
        atoms = [a for c in chains for a in c.get_atoms()
                 if a.element != "H" and a.get_parent().id[0] == " "]
    else:
        atoms = [a for c in chains for a in c.get_atoms() if a.element != "H"]
    ns = NeighborSearch(atoms)
    per = {c.id: set() for c in chains}
    contacts = set()
    for a1, a2 in ns.search_all(cutoff, level="A"):
        r1, r2 = a1.get_parent(), a2.get_parent()
        c1, c2 = r1.get_parent().id, r2.get_parent().id
        if c1 == c2:
            continue
        per[c1].add(r1.id[1])
        per[c2].add(r2.id[1])
        e1 = (c1, r1.id[1], r1.get_resname())
        e2 = (c2, r2.id[1], r2.get_resname())
        contacts.add(tuple(sorted((e1, e2))))
    return chains, per, contacts
