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
scoring_engine.inputs - Estagio 2: gera inputs de predicao a partir de (isca, parceiros).

Formatos:
  - Boltz-2: YAML `version: 1` + `sequences:` (protein A=isca, B=parceiro, + ligantes CCD).
  - AlphaFold3 Server: JSON (dialect alphafold3) para confirmacao dos top-hits.
"""
from __future__ import annotations
import json
from pathlib import Path

_LIG_IDS = "CDEFGH"


def read_fasta(path) -> str:
    return "".join(
        ln.strip() for ln in Path(path).read_text(encoding="utf-8").splitlines()
        if ln and not ln.startswith(">")
    )


# Tipos de entidade aceitos pelo Boltz (a chave YAML e o proprio tipo).
_ENTITY_TYPES = ("protein", "dna", "rna", "ligand")
_CHAIN_IDS = "ABCDEFGHIJ"


def _entity_block(entity: dict) -> list:
    """Formata UMA entidade (uma cadeia) no schema Boltz `sequences:`.

    entity: dict com
      - 'type': protein | dna | rna | ligand
      - 'id'  : identificador da cadeia (ex.: 'A'); o Boltz aceita lista para
                copias identicas (ex.: id: [A, B]) - passada crua.
      - protein/dna/rna: 'sequence' (o CONTEUDO NAO e validado aqui, por design:
                o dashboard cola FASTA cru).
      - ligand: 'ccd' (codigo CCD) XOR 'smiles'.
    """
    kind = str(entity.get("type", "")).lower()
    if kind not in _ENTITY_TYPES:
        raise ValueError(f"tipo de entidade invalido: {entity.get('type')!r} "
                         f"(use um de {_ENTITY_TYPES})")
    cid = entity.get("id")
    if cid is None or cid == "":
        raise ValueError(f"entidade {kind} sem 'id'")
    lines = [f"  - {kind}:", f"      id: {cid}"]
    if kind == "ligand":
        ccd, smiles = entity.get("ccd"), entity.get("smiles")
        if ccd:
            lines.append(f"      ccd: {ccd}")
        elif smiles:
            lines.append(f"      smiles: {smiles}")
        else:
            raise ValueError(f"ligand {cid!r} requer 'ccd' ou 'smiles'")
    else:
        lines.append(f"      sequence: {entity.get('sequence', '')}")
    return lines


def boltz_complex(entities) -> str:
    """Gera o YAML Boltz (version 1) de um complexo com entidades ARBITRARIAS
    (protein/dna/rna/ligand), na ordem dada. Gerador BASE do estagio 2; os
    atalhos boltz_yaml/boltz_chains/boltz_homomer sao casos particulares dele.
    """
    lines = ["version: 1", "sequences:"]
    for e in entities:
        lines += _entity_block(e)
    return "\n".join(lines) + "\n"


def boltz_yaml(bait_seq: str, partner_seq: str, cofactors=None) -> str:
    """Complexo isca(A)+parceiro(B) + ligantes CCD opcionais (ids a partir de C)."""
    ents = [{"type": "protein", "id": "A", "sequence": bait_seq},
            {"type": "protein", "id": "B", "sequence": partner_seq}]
    ents += [{"type": "ligand", "id": _LIG_IDS[i], "ccd": ccd}
             for i, ccd in enumerate(cofactors or [])]
    return boltz_complex(ents)


def boltz_chains(seqs, cofactors=None) -> str:
    """YAML Boltz de um complexo com cadeias arbitrarias (uma proteina por sequencia).
    Ex.: [isca, isca, parceiro] = homo-dimero da isca + parceiro."""
    ents = [{"type": "protein", "id": _CHAIN_IDS[i], "sequence": s}
            for i, s in enumerate(seqs)]
    ents += [{"type": "ligand", "id": _CHAIN_IDS[len(seqs) + j], "ccd": ccd}
             for j, ccd in enumerate(cofactors or [])]
    return boltz_complex(ents)


def boltz_homomer(seq: str, n: int, cofactors=None) -> str:
    """YAML Boltz de um homo-oligomero: n copias identicas da mesma proteina (n=1
    monomero, 2 dimero, 3 trimero...). Para estrutura/auto-associacao da isca sozinha."""
    ents = [{"type": "protein", "id": _CHAIN_IDS[i], "sequence": seq} for i in range(n)]
    ents += [{"type": "ligand", "id": _CHAIN_IDS[n + j], "ccd": ccd}
             for j, ccd in enumerate(cofactors or [])]
    return boltz_complex(ents)


def af3_json(name: str, bait_seq: str, partner_seq: str, cofactors=None) -> dict:
    seqs = [{"protein": {"id": "A", "sequence": bait_seq}},
            {"protein": {"id": "B", "sequence": partner_seq}}]
    for i, ccd in enumerate(cofactors or []):
        seqs.append({"ligand": {"id": _LIG_IDS[i], "ccdCodes": [ccd]}})
    return {"name": name, "modelSeeds": [1], "sequences": seqs,
            "dialect": "alphafold3", "version": 1}


def generate(bait_fasta, partners_dir, out_dir, cofactors=None, engine: str = "boltz"):
    """
    Gera 1 input por parceiro (FASTA em partners_dir) emparelhado com a isca.
    engine: 'boltz' | 'af3' | 'both'. Retorna a lista de nomes gerados.
    """
    bait = read_fasta(bait_fasta)
    bait_name = Path(bait_fasta).stem
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for fa in sorted(Path(partners_dir).glob("*.fasta")):
        pname = fa.stem
        if pname == bait_name:
            continue
        pseq = read_fasta(fa)
        if not pseq:
            continue
        name = f"{bait_name}__x__{pname}"
        if engine in ("boltz", "both"):
            (out / f"{name}.yaml").write_text(boltz_yaml(bait, pseq, cofactors), encoding="utf-8")
        if engine in ("af3", "both"):
            (out / f"{name}.json").write_text(
                json.dumps(af3_json(name, bait, pseq, cofactors), indent=2), encoding="utf-8")
        made.append(name)
    return made
