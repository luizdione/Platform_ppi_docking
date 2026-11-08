# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# PROVENANCE / PROCEDENCIA
#
# Reduced from the `SCORE` block of the author's own MIT-licensed package `ppi`
# (ppi_platform), by Luiz Dione Barbosa De-Melo (IFRJ). Only the screening
# thresholds the funnel reads are kept here; the machine-specific paths and
# conda-environment names of the original module were deliberately dropped.
# See README, section "Vendored scoring engine".
# ---------------------------------------------------------------------------
"""Limiares de triagem usados pelo motor de pontuacao vendorizado.

`SCORE` e lido por :mod:`scoring_engine.rank` (classificacao HIT/MAYBE/weak) e
pelo GATE 1 (:mod:`funnel.gate`), que valida as chaves na entrada. Os valores
sao limiares de TRIAGEM, nao verdades universais: ajuste-os para o seu sistema
em ``config/funnel.yaml`` (o YAML sobrepoe estes defaults).
"""
from __future__ import annotations

#: Limiares para classificar um par isca x parceiro na triagem por IA.
SCORE = {
    "iptm_hit": 0.60,       # >= forte candidato a interacao real
    "iptm_maybe": 0.50,     # zona cinzenta (vale confirmar em outro preditor)
    "pae_iface_max": 10.0,  # PAE medio de interface (A) para confiar
    "min_contacts": 10,     # contatos pesados < 5 A entre as cadeias
}

__all__ = ["SCORE"]
