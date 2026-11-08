# -*- coding: utf-8 -*-
"""Estagio "interface": extrai os residuos de contato do complexo do Boltz-2.

Posicao no funil:

    Boltz-2 (ja rodado) -> score -> GATE 1 -> [INTERFACE] -> split -> HADDOCK3

Depois que um par passa pelo GATE 1, o HADDOCK3 precisa saber QUAIS residuos
devem ser tratados como "ativos" (AIR - Ambiguous Interaction Restraints). Este
modulo le o `.cif` do Boltz e devolve exatamente esses residuos, por cadeia,
separados em ISCA e PARCEIRO.

Nada e reimplementado: a busca de vizinhanca vem de `scoring_engine._common.interface_pairs`
(mesma funcao que alimenta `scoring_engine.score`), de modo que o numero de contatos daqui
bate com o `n_contacts` do ranking do motor. O import do motor e PREGUICOSO
(dentro das funcoes) para este modulo poder ser importado num ambiente sem
biopython/numpy sem explodir na hora do import.

Quem decide os papeis: a CAMPANHA declara, a posicao so serve de fallback
------------------------------------------------------------------------
A convencao GLOBAL do motor - ISCA = todas as cadeias PROTEICAS iniciais,
PARCEIRO = a ULTIMA cadeia PROTEICA - e **apenas o FALLBACK** deste modulo, para
o caso de a campanha nao declarar nada. Ela e POSICIONAL, nao biologica.

Quando o subprojeto declara o bloco ``chain_roles`` (em
``config/subproject.*.yaml``), **a declaracao vence** e a posicao no arquivo e
ignorada: passe o bloco em ``chain_roles(cif, esperado=...)`` e os papeis
resultantes em ``iface_residues(..., bait_chains=..., partner_chain=...)`` /
``split.split_complex(..., esperado=...)``.

Por que isso importa (DEMO01 barnase, verificado no dado real): o `.cif` tem
A = barstar (760 res), B = barnase (376 res), C = ATP, D = MG. O fallback
posicional elegeria a BARSTAR como isca/receptor e a BARNASE como parceiro -
exatamente o inverso do que a campanha declara (receptor = barnase = cadeia B,
ligante = barstar = cadeia A). Com os papeis declarados, `chain_roles` ainda
CONFERE tamanho e existencia das cadeias e falha alto se divergir
(``fail_if_ambiguous``): nunca se chuta pela ordem.

A eleicao (declarada ou posicional) ignora cadeias sem residuo padrao (ATP, MG,
agua): um "ultima cadeia" literal elegeria o magnesio como parceiro.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

__all__ = ["chain_roles", "declared_roles", "iface_residues"]

#: Tolerancia (em residuos) na conferencia de ``n_residues_esperado``. Existe
#: porque o numero anotado no YAML vem de leitura do modelo e pode oscilar
#: por 1-2 residuos entre modelos do mesmo par (extremidades nao resolvidas).
TOLERANCIA_RESIDUOS = 2


# --------------------------------------------------------------------------
# helpers internos
# --------------------------------------------------------------------------
def _check_cif(cif_path: "str | Path") -> Path:
    """Valida o caminho do .cif (existencia e extensao). Falha alto e claro."""
    caminho = Path(cif_path)
    if not caminho.is_file():
        raise FileNotFoundError(f"modelo .cif nao encontrado: {caminho}")
    if caminho.suffix.lower() not in {".cif", ".mmcif"}:
        raise ValueError(
            f"esperava um arquivo mmCIF (.cif) do Boltz, veio {caminho.name!r}. "
            "Nao use .pdb aqui: o parser de PDB do biopython le .cif com ZERO "
            "atomos em silencio."
        )
    return caminho


def _protein_chain_ids(model) -> "Tuple[List[str], List[int]]":
    """(ids, tamanhos) das cadeias com pelo menos 1 residuo PADRAO (proteicas).

    Delega para `scoring_engine._common.chain_ids_sizes`, que ja descarta cadeias formadas
    so por HETATM (cofatores como ATP/MG) e aguas.
    """
    from scoring_engine import _common

    return _common.chain_ids_sizes(model)


# --------------------------------------------------------------------------
# papeis DECLARADOS pela campanha (bloco chain_roles do YAML)
# --------------------------------------------------------------------------
def _papel_declarado(bloco: "Any", papel: str) -> "Dict[str, Any]":
    """Normaliza um sub-bloco ``receptor``/``ligand`` do YAML da campanha."""
    if not isinstance(bloco, Mapping):
        raise ValueError(
            f"chain_roles.{papel}: esperava um mapeamento com ao menos 'chain', "
            f"veio {type(bloco).__name__}"
        )
    cadeia = bloco.get("chain")
    if cadeia is None or not str(cadeia).strip():
        raise ValueError(f"chain_roles.{papel}: falta o campo obrigatorio 'chain'")
    exato = bloco.get("n_residues_esperado")
    minimo = bloco.get("n_residues_esperado_min")
    return {
        "chain": str(cadeia).strip(),
        "molecule": (str(bloco["molecule"]).strip() if bloco.get("molecule") else None),
        "n_exato": (int(exato) if exato is not None else None),
        "n_min": (int(minimo) if minimo is not None else None),
    }


def declared_roles(esperado: "Mapping[str, Any]") -> "Dict[str, Any]":
    """Traduz o bloco ``chain_roles`` do YAML da campanha para papeis do funil.

    Entrada = o bloco tal como esta em ``config/subproject.*.yaml``::

        chain_roles:
          receptor: {molecule: barnase,  chain: B, n_residues_esperado: 376}
          ligand:   {molecule: barstar, chain: A, n_residues_esperado_min: 600}
          cofactors: [{chain: C, comp: ATP}, {chain: D, comp: MG}]
          fail_if_ambiguous: true

    Saida (vocabulario deste modulo; RECEPTOR do HADDOCK == ISCA/bait, que e o
    que vira ``manifest.bait``/``receptor_pdb``)::

        {
          "bait_chains": ["B"], "partner_chain": "A",
          "bait_molecule": "barnase", "partner_molecule": "barstar",
          "cofactor_chains": ["C", "D"],
          "esperado": {"B": {"exato": 376, "min": None}, "A": {...}},
          "fail_if_ambiguous": True,
        }

    Levanta ``ValueError`` se o bloco nao trouxer ``receptor``/``ligand`` com
    ``chain``, ou se as duas cadeias forem a mesma. Nao toca em disco: a
    conferencia contra o modelo real e feita em `chain_roles`.
    """
    if not isinstance(esperado, Mapping):
        raise ValueError(
            f"chain_roles declarado: esperava um mapeamento, veio {type(esperado).__name__}"
        )
    faltando = [k for k in ("receptor", "ligand") if k not in esperado]
    if faltando:
        raise ValueError(
            f"bloco chain_roles incompleto: falta {faltando} "
            f"(chaves presentes: {sorted(esperado)})"
        )
    receptor = _papel_declarado(esperado["receptor"], "receptor")
    ligante = _papel_declarado(esperado["ligand"], "ligand")
    if receptor["chain"] == ligante["chain"]:
        raise ValueError(
            f"bloco chain_roles: receptor e ligand apontam a MESMA cadeia "
            f"{receptor['chain']!r}"
        )
    cofatores: "List[str]" = []
    for item in esperado.get("cofactors") or []:
        if isinstance(item, Mapping) and item.get("chain"):
            cofatores.append(str(item["chain"]).strip())
        elif isinstance(item, str) and item.strip():
            cofatores.append(item.strip())
    return {
        "bait_chains": [receptor["chain"]],
        "partner_chain": ligante["chain"],
        "bait_molecule": receptor["molecule"],
        "partner_molecule": ligante["molecule"],
        "cofactor_chains": cofatores,
        "esperado": {
            receptor["chain"]: {"exato": receptor["n_exato"], "min": receptor["n_min"]},
            ligante["chain"]: {"exato": ligante["n_exato"], "min": ligante["n_min"]},
        },
        "fail_if_ambiguous": bool(esperado.get("fail_if_ambiguous", True)),
    }


def _conferir_declarado(
    nome_arquivo: str, tamanhos: "Dict[str, int]", papeis: "Dict[str, Any]"
) -> None:
    """Confere os papeis DECLARADOS contra o modelo real (existencia + tamanho).

    Cadeia declarada que nao existe (ou nao e proteica) e SEMPRE ``ValueError``:
    seguir com ela produziria PDB vazio ou papel trocado. Tamanho fora do
    esperado tambem levanta quando ``fail_if_ambiguous`` (o caso do DEMO01); com
    ``fail_if_ambiguous: false`` vira ``RuntimeWarning`` explicito.
    """
    import warnings

    declaradas = [*papeis["bait_chains"], papeis["partner_chain"]]
    ausentes = [c for c in declaradas if c not in tamanhos]
    if ausentes:
        raise ValueError(
            f"{nome_arquivo}: a campanha declara a(s) cadeia(s) {ausentes}, que nao "
            f"existe(m) como cadeia proteica neste modelo. Cadeias proteicas "
            f"disponiveis (id: n_residuos): {tamanhos}. NAO vou eleger papeis pela "
            "ordem das cadeias para contornar isso - corrija o bloco chain_roles do "
            "YAML ou confira se este .cif e mesmo o do par declarado."
        )

    problemas: "List[str]" = []
    for cadeia, limites in (papeis.get("esperado") or {}).items():
        if cadeia not in tamanhos:
            continue
        n = tamanhos[cadeia]
        exato = limites.get("exato")
        minimo = limites.get("min")
        if exato is not None and abs(n - int(exato)) > TOLERANCIA_RESIDUOS:
            problemas.append(
                f"cadeia {cadeia}: {n} residuos, esperado {exato} "
                f"(tolerancia +/-{TOLERANCIA_RESIDUOS})"
            )
        if minimo is not None and n < int(minimo):
            problemas.append(f"cadeia {cadeia}: {n} residuos, esperado >= {minimo}")

    if not problemas:
        return
    mensagem = (
        f"{nome_arquivo}: os papeis declarados pela campanha NAO batem com o modelo "
        f"({'; '.join(problemas)}). Tamanhos reais: {tamanhos}. As cadeias podem "
        "estar em outra ordem neste run, ou o .cif nao e o do par declarado."
    )
    if papeis.get("fail_if_ambiguous", True):
        raise ValueError(
            mensagem + " (fail_if_ambiguous=true: erro em vez de chute pela ordem)"
        )
    warnings.warn(
        mensagem + " (fail_if_ambiguous=false: seguindo com o papel declarado)",
        RuntimeWarning,
        stacklevel=3,
    )


# --------------------------------------------------------------------------
# eleicao de papeis (isca x parceiro): declarada pela campanha ou posicional
# --------------------------------------------------------------------------
def chain_roles(
    cif_path: "str | Path", *, esperado: "Mapping[str, Any] | None" = None
) -> "Dict[str, Any]":
    """Elege ISCA (receptor) e PARCEIRO (ligante) de um complexo.

    ``esperado`` = o bloco ``chain_roles`` do YAML da campanha (ver
    `declared_roles`). Quando informado, ele **manda**: os papeis saem da
    declaracao e sao CONFERIDOS contra o modelo (existencia das cadeias +
    ``n_residues_esperado``/``n_residues_esperado_min``, honrando
    ``fail_if_ambiguous``). Sem ele, vale o FALLBACK posicional do motor
    (isca = cadeias proteicas iniciais, parceiro = a ULTIMA), que e uma
    convencao de ordem, nao de biologia - ver o cabecalho do modulo.

    Retorna::

        {
          "bait_chains":    ["B", ...],   # receptor/isca
          "partner_chain":  "A",          # parceiro/ligante
          "chain_sizes":    {"A": 760, "B": 376},   # residuos padrao por cadeia
          "hetero_chains":  ["C", "D"],   # cadeias sem residuo padrao (ATP/MG/agua)
          "fonte":          "declarado" | "posicional",
          # so quando declarado:
          "bait_molecule": "barnase", "partner_molecule": "barstar",
          "cofactor_chains": ["C", "D"],
        }

    Levanta ``ValueError`` se houver menos de 2 cadeias proteicas (um monomero
    nao tem interface para dar ao HADDOCK) ou se a declaracao divergir do modelo.
    """
    from scoring_engine import _common

    caminho = _check_cif(cif_path)
    model = _common.parse_model(caminho)
    ids, sizes = _protein_chain_ids(model)
    todas = [c.id for c in model]

    if len(ids) < 2:
        raise ValueError(
            f"{caminho.name}: o modelo tem apenas {len(ids)} cadeia(s) proteica(s) "
            f"({ids or 'nenhuma'}); cadeias presentes no arquivo: {todas}. "
            "Sao necessarias >= 2 cadeias proteicas para extrair uma interface "
            "isca x parceiro. Se o arquivo parece correto, verifique se o .cif "
            "nao esta truncado/vazio."
        )

    tamanhos = {cid: n for cid, n in zip(ids, sizes)}
    comum = {
        "chain_sizes": tamanhos,
        "hetero_chains": [c for c in todas if c not in ids],
    }

    if esperado is None:
        return {
            "bait_chains": list(ids[:-1]),
            "partner_chain": ids[-1],
            "fonte": "posicional",
            **comum,
        }

    papeis = declared_roles(esperado)
    _conferir_declarado(caminho.name, tamanhos, papeis)
    return {
        "bait_chains": list(papeis["bait_chains"]),
        "partner_chain": papeis["partner_chain"],
        "fonte": "declarado",
        "bait_molecule": papeis["bait_molecule"],
        "partner_molecule": papeis["partner_molecule"],
        "cofactor_chains": list(papeis["cofactor_chains"]),
        **comum,
    }


# --------------------------------------------------------------------------
# extracao dos residuos de interface (viram os AIRs do HADDOCK3)
# --------------------------------------------------------------------------
def iface_residues(
    cif_path: "str | Path",
    cutoff: float = 5.0,
    *,
    protein_only: bool = True,
    strict: bool = True,
    bait_chains: "Sequence[str] | None" = None,
    partner_chain: "str | None" = None,
) -> "Dict[str, Any]":
    """Residuos de interface ISCA x PARCEIRO de um complexo `.cif` do Boltz.

    Parametros
    ----------
    cif_path : caminho do modelo (ex.: ``..._model_0.cif``).
    cutoff : distancia (A) entre atomos PESADOS para contar contato (default 5.0,
        o mesmo do `scoring_engine.score`).
    protein_only : quando True (default), residuos HETERO (ATP, MG, agua) sao
        EXCLUIDOS da busca de vizinhanca. Sem isso, um residuo da isca que so
        toca o nucleotideo entraria na lista de AIRs como se fosse contato
        proteina-proteina. Deixe False apenas se quiser reproduzir o numero
        historico de `scoring_engine.score` (que nao filtra).
    bait_chains, partner_chain : papeis EXPLICITOS (tipicamente vindos de
        ``chain_roles(cif, esperado=<bloco do YAML>)``). Quando informados, eles
        **mandam** - a convencao posicional do motor nao e consultada. As
        cadeias precisam existir no modelo E ser proteicas; caso contrario o
        erro e claro, listando as cadeias disponiveis. Informar so um dos dois
        tambem e aceito: o outro papel continua vindo do fallback posicional.
    strict : quando True (default), interface VAZIA (nenhum contato entre isca e
        parceiro) levanta ``ValueError`` em vez de devolver listas vazias -
        AIRs vazios fariam o HADDOCK3 rodar sem restricao nenhuma e produzir um
        resultado plausivel e errado. Passe False so para inspecao/diagnostico.

    Retorno
    -------
    dict com::

        {
          "bait_chains":      ["A"],          # cadeias proteicas iniciais
          "partner_chain":    "B",            # ultima cadeia proteica
          "bait_residues":    [12, 15, ...],  # numeros de residuo (auth_seq_id)
          "partner_residues": [30, 31, ...],
          "n_pairs":          29,             # pares residuo-residuo isca<->parceiro
          "cutoff":           5.0,
          "protein_only":     True,
          "chain_sizes":      {"A": 760, "B": 376},
          "hetero_chains":    ["C", "D"],
        }

    Os numeros sao os do modelo (biopython ``residue.id[1]``, i.e. `auth_seq_id`
    do mmCIF) - a mesma numeracao que deve ir para o `ambig.tbl` do HADDOCK3,
    ja que os PDBs entregues ao docking sao escritos a partir DESTE mesmo modelo
    (ver ``funnel.split``).
    """
    from scoring_engine import _common

    caminho = _check_cif(cif_path)
    model = _common.parse_model(caminho)

    ids, sizes = _protein_chain_ids(model)
    todas = [c.id for c in model]
    if len(ids) < 2:
        raise ValueError(
            f"{caminho.name}: o modelo tem apenas {len(ids)} cadeia(s) proteica(s) "
            f"({ids or 'nenhuma'}); cadeias presentes no arquivo: {todas}. "
            "Sao necessarias >= 2 cadeias proteicas para extrair uma interface."
        )

    tamanhos = {cid: n for cid, n in zip(ids, sizes)}
    # papel explicito MANDA; a convencao posicional e so o fallback.
    escolhidas_bait = (
        [str(c) for c in bait_chains] if bait_chains else list(ids[:-1])
    )
    escolhida_partner = str(partner_chain) if partner_chain else ids[-1]
    explicito = bool(bait_chains or partner_chain)
    if explicito:
        desconhecidas = [
            c for c in [*escolhidas_bait, escolhida_partner] if c not in tamanhos
        ]
        if desconhecidas:
            raise ValueError(
                f"{caminho.name}: papel de cadeia explicito aponta {desconhecidas}, "
                f"que nao existe(m) como cadeia proteica neste modelo. Cadeias "
                f"proteicas (id: n_residuos): {tamanhos}; demais cadeias do arquivo: "
                f"{[c for c in todas if c not in tamanhos]}."
            )
        if escolhida_partner in escolhidas_bait:
            raise ValueError(
                f"{caminho.name}: a cadeia {escolhida_partner!r} foi indicada como "
                "isca E como parceiro ao mesmo tempo"
            )
    bait_chains = escolhidas_bait
    partner_chain = escolhida_partner

    # busca UNICA de vizinhanca, reusando a implementacao do motor
    _chains, _per, contatos = _common.interface_pairs(
        model, cutoff=cutoff, protein_only=protein_only
    )

    # `_per` mistura TODAS as cadeias (inclusive contatos isca-isca em oligomero
    # e contatos com cofator quando protein_only=False). Aqui filtramos so os
    # pares que cruzam a fronteira ISCA <-> PARCEIRO.
    bait_set = set(bait_chains)
    bait_res: set[int] = set()
    partner_res: set[int] = set()
    n_pairs = 0
    for (c1, r1, _n1), (c2, r2, _n2) in contatos:
        if c1 in bait_set and c2 == partner_chain:
            bait_res.add(r1)
            partner_res.add(r2)
            n_pairs += 1
        elif c2 in bait_set and c1 == partner_chain:
            bait_res.add(r2)
            partner_res.add(r1)
            n_pairs += 1

    if strict and n_pairs == 0:
        raise ValueError(
            f"{caminho.name}: NENHUM contato < {cutoff} A entre a isca "
            f"{bait_chains} e o parceiro {partner_chain!r} "
            f"(protein_only={protein_only}). Sem residuos ativos o HADDOCK3 "
            "rodaria sem restricao ambigua e devolveria uma pose sem sentido. "
            "Confira se este .cif e mesmo o complexo esperado, ou chame com "
            "strict=False se a intencao e apenas inspecionar."
        )

    return {
        "bait_chains": bait_chains,
        "partner_chain": partner_chain,
        "bait_residues": sorted(bait_res),
        "partner_residues": sorted(partner_res),
        "n_pairs": n_pairs,
        "cutoff": float(cutoff),
        "protein_only": bool(protein_only),
        "chain_sizes": tamanhos,
        "hetero_chains": [c for c in todas if c not in ids],
        "fonte": "explicito" if explicito else "posicional",
    }
