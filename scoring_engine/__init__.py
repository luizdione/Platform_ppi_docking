# -*- coding: utf-8 -*-
"""Vendored scoring engine: confidence metrics for a predicted protein complex.

PROVENANCE / PROCEDENCIA
------------------------
These modules are a verbatim, trimmed copy of the modules the funnel imports
from the author's own MIT-licensed package ``ppi`` (``ppi_platform``), by
Luiz Dione Barbosa De-Melo (IFRJ, ORCID 0000-0003-2445-9943). They are vendored
so that this repository runs standalone, without the full private package
installed. Every file carries its own provenance header.

Vendored here, and nothing else:

* :mod:`scoring_engine._common` -- ``.npz``/``.cif`` readers and the single
  inter-chain interface search (biopython ``NeighborSearch``);
* :mod:`scoring_engine.ipsae` -- ipSAE, the interface score that is robust to
  disordered regions and mobile domains (Dunbrack et al., 2025);
* :mod:`scoring_engine.score` -- per-complex metrics (ipTM, interface ipTM,
  pTM, ipSAE, complex pLDDT, interface PAE, interface pLDDT, BSA, contacts),
  for 1:1 complexes and for oligomers;
* :mod:`scoring_engine.rank` -- HIT/MAYBE/weak classification and the ranked
  table/shortlist;
* :mod:`scoring_engine.config` -- the screening thresholds (``SCORE``);
* :mod:`scoring_engine.inputs` -- Boltz-2 / AlphaFold3 input generation.

``config`` and ``inputs`` are stdlib-only and imported eagerly; everything else
needs numpy/biopython and must be imported explicitly, so that
``import scoring_engine`` stays cheap in a light environment::

    from scoring_engine import score, rank   # requires numpy + biopython
"""

from __future__ import annotations

from . import config, inputs  # noqa: F401  (stdlib-only, always available)

__all__ = ["config", "inputs"]
