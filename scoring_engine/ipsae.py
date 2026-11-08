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
ipSAE — interface score robusto a regioes desordenadas / dominios moveis (Dunbrack et al., 2025).
O ipTM global e rebaixado por dominios que NAO interagem e por regioes desordenadas, mesmo quando
a interface real e boa. O ipSAE recomputa um pTM restrito aos pares INTER-cadeia com PAE < cutoff,
ignorando o resto -> reflete a confianca da interface local.

Entrada: matriz PAE (NxN) do modelo + n1 = n. de tokens da cadeia A (isca). Cadeia A = [0:n1], B = [n1:N].
"""
from __future__ import annotations
import numpy as np


def _d0(n: int) -> float:
    n = max(int(n), 19)
    return max(1.0, 1.24 * (n - 15) ** (1.0 / 3.0) - 1.8)


def ipsae(pae, n1: int, pae_cutoff: float = 15.0) -> float:
    pae = np.asarray(pae, dtype=float)

    def asym(block):
        best = 0.0
        for i in range(block.shape[0]):
            row = block[i]
            m = row < pae_cutoff
            n0 = int(m.sum())
            if n0 == 0:
                continue
            d0 = _d0(n0)
            v = float(np.mean(1.0 / (1.0 + (row[m] / d0) ** 2)))
            if v > best:
                best = v
        return best

    ab = pae[:n1, n1:]   # linhas = cadeia A, colunas = cadeia B
    ba = pae[n1:, :n1]   # linhas = cadeia B, colunas = cadeia A
    return max(asym(ab), asym(ba))
