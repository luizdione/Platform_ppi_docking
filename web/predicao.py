# -*- coding: utf-8 -*-
"""Nova predicao: resolve as sequencias e escreve o YAML de entrada do Boltz-2.

Esta e a porta de entrada de dado NOVO na plataforma. O trabalho aqui e rapido
(buscar uma sequencia por accession leva segundos), entao roda na propria
requisicao -- diferente da predicao em si, que e job de GPU.

Regra herdada do projeto e mantida: **nunca confiar em sequencia colada a mao
quando existe accession**. Pedir por UniProt baixa a canonica; colar texto e
aceito, mas fica registrado no YAML de onde veio cada cadeia.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

_NOME_SEGURO = re.compile(r"^[A-Za-z0-9_.-]+$")

#: tipos de cadeia aceitos (espelham `scoring_engine.inputs.boltz_complex`)
TIPOS = ["protein", "dna", "rna", "ligand"]

#: como o valor de cada cadeia e obtido
FONTES = {
    "sequencia": "colar a sequencia (ou o SMILES/CCD, para ligante)",
    "uniprot": "baixar a canonica por accession UniProt",
}


def _entidade(cadeia: str, tipo: str, valor: str) -> "Dict[str, Any]":
    """Uma entidade no contrato de `scoring_engine.inputs.boltz_complex`.

    `protein`/`dna`/`rna` levam a sequencia; `ligand` leva um codigo CCD (3-4
    caracteres alfanumericos, ex.: ATP, MG, GTP) ou um SMILES. A distincao e
    feita pelo FORMATO do valor, nao por um campo extra na tela: um CCD e curto
    e alfanumerico, um SMILES nao.
    """
    if tipo == "ligand":
        bruto = valor.strip()
        if 1 <= len(bruto) <= 4 and bruto.isalnum():
            return {"type": tipo, "id": cadeia, "ccd": bruto.upper()}
        return {"type": tipo, "id": cadeia, "smiles": bruto}
    return {"type": tipo, "id": cadeia, "sequence": valor}


def _resolver_valor(fonte: str, valor: str) -> str:
    """Devolve a sequencia final da cadeia, baixando quando a fonte e accession."""
    valor = (valor or "").strip()
    if not valor:
        raise ValueError("cadeia sem valor")
    if fonte == "sequencia":
        return valor
    if fonte == "uniprot":
        from web.motor_ppi import core

        # `resolve_sequence` do motor ja trata o fallback UniParc
        return core().resolve_sequence("uniprot", valor)
    raise ValueError(f"fonte desconhecida: {fonte!r}")


def montar_yaml(
    nome: str,
    entidades: "List[Dict[str, str]]",
    destino: str,
) -> "Dict[str, Any]":
    """Escreve `<destino>/<nome>.yaml` no dialeto do Boltz-2.

    `entidades` = [{cadeia, tipo, fonte, valor}, ...]. Erra alto (e explica) em
    vez de gerar um YAML pela metade: um input torto so descobre na GPU, horas
    depois.
    """
    nome = (nome or "").strip()
    # A whitelist ja barra `/`, `\` e caminho relativo; o que ela deixava passar
    # era o nome feito SO de pontos ("." ou ".."), que gera um arquivo esquisito
    # (`...yaml`) dentro da pasta. Nao e fuga de diretorio, mas tambem nao e nome
    # de predicao nenhum.
    if not _NOME_SEGURO.match(nome) or not nome.strip("."):
        raise ValueError(
            f"nome invalido: {nome!r} (use letras, numeros, '_', '-' e '.')"
        )
    if not entidades:
        raise ValueError("nenhuma cadeia informada")
    pasta = Path(destino)
    if not pasta.is_dir():
        raise ValueError(f"pasta de destino nao existe: {pasta}")

    resolvidas: "List[Dict[str, Any]]" = []
    for i, ent in enumerate(entidades, start=1):
        cadeia = (ent.get("cadeia") or "").strip().upper() or chr(ord("A") + i - 1)
        tipo = (ent.get("tipo") or "protein").strip()
        if tipo not in TIPOS:
            raise ValueError(f"tipo invalido na cadeia {cadeia}: {tipo!r}")
        fonte = (ent.get("fonte") or "sequencia").strip()
        try:
            sequencia = _resolver_valor(fonte, ent.get("valor", ""))
        except Exception as exc:  # noqa: BLE001 - explica QUAL cadeia falhou
            raise ValueError(f"cadeia {cadeia}: {exc}") from exc
        resolvidas.append(
            {
                "cadeia": cadeia,
                "tipo": tipo,
                "fonte": fonte,
                "origem": ent.get("valor", "").strip() if fonte != "sequencia" else "",
                "n_res": len(sequencia),
                "_entidade": _entidade(cadeia, tipo, sequencia),
            }
        )

    from scoring_engine import inputs as engine_inputs

    texto = engine_inputs.boltz_complex([r.pop("_entidade") for r in resolvidas])
    alvo = pasta / f"{nome}.yaml"
    alvo.write_text(texto, encoding="utf-8")
    return {
        "yaml": str(alvo),
        "bytes": alvo.stat().st_size,
        "cadeias": resolvidas,
    }
