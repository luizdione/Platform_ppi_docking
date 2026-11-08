# -*- coding: utf-8 -*-
"""Pontua UM subprojeto e grava o manifesto no cache. Roda como PROCESSO.

    python -m web.scorer <key> <arquivo_de_saida>

Por que processo e nao thread: pontuar e trabalho de CPU em Python puro
(`scoring_engine.score` + parser de interface). Numa thread, o GIL faz o servidor inteiro
engasgar -- medido: uma pagina ja cacheada levando >30 s para responder enquanto
o aquecimento rodava. Num processo separado o servidor continua leve, e de
quebra ganha-se cancelamento de verdade (matar o PID) e isolamento de falha.

Escreve o JSON no caminho pedido de forma ATOMICA (arquivo temporario + rename),
para que o servidor nunca leia um manifesto pela metade.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


def main(argv: "list[str]") -> int:
    if len(argv) != 2:
        print("uso: python -m web.scorer <key> <arquivo_saida>", file=sys.stderr)
        return 2
    key, destino_txt = argv
    destino = Path(destino_txt)

    from core import data as core

    subs = [s for s in core.load_subprojects() if s.key == key]
    if not subs:
        print(f"subprojeto desconhecido: {key!r}", file=sys.stderr)
        return 3
    manifesto = core.pairs_manifest(subs[0])

    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_suffix(destino.suffix + f".{os.getpid()}.tmp")
    temporario.write_text(json.dumps(manifesto, ensure_ascii=False), encoding="utf-8")
    os.replace(temporario, destino)  # atomico: o servidor nunca le pela metade
    print(f"OK {key}: {len(manifesto.get('pairs', []))} pares -> {destino}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
