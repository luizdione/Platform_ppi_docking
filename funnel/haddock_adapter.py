# -*- coding: utf-8 -*-
"""Ponte entre o `PairManifest` do funil e o `Haddock3Engine` JA EXISTENTE.

Este modulo **nao reimplementa** nada do HADDOCK3: ele apenas traduz o nosso
envelope (`funnel.contract.PairManifest`) para o contrato de entrada do motor
que vive no repo vizinho `docking_suite_platform` (`engines/haddock3.py`), e
traduz a saida de volta para `HaddockArtifacts`.

Divisao de responsabilidade
---------------------------
* Quem escolhe os residuos de interface e o modulo `interface`/`split` do funil
  (a montante). Aqui eles chegam prontos no manifesto.
* Quem escreve `.cfg` e `ambig.tbl` e o `Haddock3Engine.prepare` -- que e
  **logica pura**: funciona SEM o binario `haddock3` instalado.
* Quem roda o docking de fato e `Haddock3Engine.run` -- que **exige** o binario.
  Onde ele nao existe (Windows, por exemplo), o erro e propagado inteiro,
  nunca mascarado (ver `run`), e o caminho normal e o container.

Convencao de cadeias/segids (CONTRATO COM O ESTAGIO ANTERIOR)
-------------------------------------------------------------
O `build_ambig_tbl` do motor escreve os segids **fixos**: receptor = ``A``,
parceiro = ``C``. Logo, o PDB apontado por ``manifest.receptor_pdb`` (a ISCA,
``manifest.bait``) precisa ter segid/chain ``A``, e o de ``manifest.ligand_pdb``
(o PARCEIRO, ``manifest.partner``) precisa ter segid/chain ``C``. Quem faz o
split CIF->PDB e responsavel por renomear as cadeias para esse par A/C -- a
ordem original das cadeias no `.cif` do Boltz **nao** e confiavel (no DEMO01
barnase a isca barnase esta na cadeia B e a barstar parceira na A).

Ressalva de NUMERACAO de residuo (honestidade obrigatoria)
----------------------------------------------------------
Os numeros em ``active_residues``/``partner_active_residues`` sao lidos da
estrutura do Boltz. A numeracao gravada no PDB gerado pelo split **pode diferir**
da do `.cif` de origem (renumeracao do `PDBIO`, insercoes, offsets de autor).
O proprio header do `ambig.tbl` gerado pelo motor avisa disso
("Numeracao = a do registry; pode exigir ajuste a numeracao do PDB"). Este
adaptador **nao adivinha** nenhum ajuste: se houver deslocamento, o AIR aponta
para o residuo errado em silencio dentro do HADDOCK3. Confira o `.tbl` contra o
PDB antes de rodar (`funnel` deve gerar ambos a partir do MESMO objeto de
estrutura para que a numeracao case por construcao).

Por que passar `caprieval_reference` -- e por que ela e um PDB, nunca o `.cif`
-----------------------------------------------------------------------------
Sem `reference_fname`, o modulo `caprieval` do HADDOCK3 fica
**auto-referenciado**: ele compara os clusters contra o proprio melhor modelo do
HADDOCK, e o cluster de `caprieval_rank=1` sai com `irmsd=0.0` / `dockq=1.0`
**por construcao** -- numeros bonitos e completamente vazios de significado.
Com a referencia certa, o `capri_clt.tsv` final passa a trazer DockQ/iRMSD
**contra a predicao do Boltz**, que e exatamente a pergunta do GATE 2: "o docking
fisico reproduz a pose predita?". So esse DockQ pode ser entregue a
`haddock_analysis.decide`.

A referencia, porem, **nao pode ser o `.cif` do Boltz**. O haddock3 le
`reference_fname` como PDB de COLUNAS FIXAS (`libpdb.slc_resseq = slice(22,26)`,
`libalign.pdb2fastadic:884` fazendo `int(line[22:26])`) e nao tem parser de mmCIF
em lugar nenhum; passar o `.cif` levanta ``invalid literal for int() with base
10: ' A -'`` dentro de cada worker, que o `libparallel` engole devolvendo
``None``, ate estourar como ``AttributeError: 'NoneType' object has no attribute
'identificator'`` -- foi exatamente o que derrubou o smoke de rigidbody do DEMO01.
Por isso este adaptador converte o `.cif` em
``pdbs/reference.pdb`` (complexo inteiro, receptor ``A`` + ligante ``C``, sem
ATP/MG) via `funnel.split.reference_pdb`, e passa ESSE arquivo. Se a referencia
nao puder ser gerada/conferida, o erro sobe -- nunca se volta ao `.cif` nem se
omite `reference_fname` (omitir devolveria o DockQ auto-referenciado acima).

Consequencia pratica ao LER o resultado: os `modules` default do motor ja
incluem `caprieval` duas vezes, e o `caprieval_reference` acrescenta um TERCEIRO
bloco no fim. O `.cfg` sai com tres secoes `[caprieval]` -- as duas primeiras
**sem** `reference_fname`, ou seja, auto-referenciadas. Ao coletar o
`capri_clt.tsv`, aponte sempre para o diretorio `analysis/` do **ultimo** modulo
caprieval do `run_dir`; qualquer outro traria o DockQ auto-referenciado que a
propria docstring de `haddock_analysis.decide` proibe.

Cuidado de sintaxe: o motor interpola o caminho **cru** entre aspas duplas no
`.cfg`. Um caminho Windows como ``D:\\dados\\...`` viraria ``\\d`` dentro do
arquivo. Por isso `build_request` normaliza todos os caminhos para barra normal.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from funnel.contract import HaddockArtifacts, PairManifest

if TYPE_CHECKING:  # pragma: no cover - so para o type checker
    from engines.base import EngineRequest, EngineResult

__all__ = [
    "DOCKING_SUITE_HOME_ENV",
    "DEFAULT_DOCKING_SUITE_HOME",
    "docking_suite_home",
    "ensure_split",
    "ensure_reference",
    "build_request",
    "prepare",
    "run",
    "check_toolchain",
    "run_dir_name",
    "expected_run_dir",
]

# Raiz do repositorio que fornece `engines.haddock3`. Esse motor vive num
# repositorio PROPRIO e publico (Platform_docking_suite) - nao e duplicado
# aqui. Resolucao: env DOCKING_SUITE_HOME; sem ela, procura o repositorio
# clonado AO LADO deste (irmao na mesma pasta), que e o layout sugerido no
# README. Nunca um caminho absoluto de uma maquina especifica.
DOCKING_SUITE_HOME_ENV = "DOCKING_SUITE_HOME"
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOCKING_SUITE_HOME = _REPO_ROOT.parent / "Platform_docking_suite"
DOCKING_SUITE_URL = "https://github.com/luizdione/Platform_docking_suite"

# Segids esperados pelo build_ambig_tbl do motor (ver docstring do modulo).
RECEPTOR_SEGID = "A"
PARTNER_SEGID = "C"

# Nome-base do PDB de referencia do caprieval, gravado ao lado dos PDBs do par.
REFERENCE_NAME = "reference"

_NOME_SEGURO = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------
# 1. importabilidade dos engines do repo vizinho
# --------------------------------------------------------------------------
def docking_suite_home() -> Path:
    """Raiz do repositorio dos engines de docking.

    Ordem: env ``DOCKING_SUITE_HOME`` -> repositorio irmao
    ``../Platform_docking_suite``. Sempre devolve um ``Path`` (mesmo que nao
    exista): quem checa a existencia e ``_ensure_engines_importable``.
    """
    bruto = os.environ.get(DOCKING_SUITE_HOME_ENV, '').strip()
    return Path(bruto) if bruto else Path(DEFAULT_DOCKING_SUITE_HOME)


def _ensure_engines_importable() -> Path:
    """Garante que `import engines.*` funcione; erro claro se nao der.

    O pacote `engines` NAO e instalavel (o `pyproject.toml` do repo vizinho so
    empacota `haddock_analysis` e `haddock_config`), e `engines/haddock3.py` usa
    import ABSOLUTO (`from engines import toolchain`) -- portanto e a **raiz do
    repo** que precisa entrar no `sys.path`, nao a pasta `engines/`.

    Retorna a raiz usada. Idempotente. Levanta:

    * ``FileNotFoundError`` se a pasta (ou o subpacote `engines/`) nao existir;
    * ``ImportError`` se a pasta existir mas o import falhar assim mesmo.
    """
    raiz = docking_suite_home()
    if not raiz.is_dir():
        raise FileNotFoundError(
            f"repositorio dos engines de docking nao encontrado em {raiz}.\n"
            f"Clone-o ({DOCKING_SUITE_URL}) ao lado deste repositorio, ou "
            f"defina a variavel de ambiente {DOCKING_SUITE_HOME_ENV} com a raiz "
            "do clone."
        )
    pacote = raiz / "engines"
    if not (pacote / "__init__.py").is_file():
        raise FileNotFoundError(
            f"{raiz} existe mas nao contem o pacote 'engines/' "
            "(caminho aponta para o repo errado?)"
        )
    texto = str(raiz)
    if texto not in sys.path:
        sys.path.insert(0, texto)
    try:
        import engines  # noqa: F401
        from engines import haddock3  # noqa: F401
    except Exception as exc:  # pragma: no cover - depende do repo vizinho
        raise ImportError(
            f"'engines' esta em {raiz} mas nao importou: {exc!r}"
        ) from exc
    return raiz


def _engine():
    """Instancia o `Haddock3Engine` registrado (nunca uma copia local)."""
    _ensure_engines_importable()
    from engines import get_engine

    return get_engine("haddock3")


def _request_cls():
    """Classe `EngineRequest` do repo vizinho."""
    _ensure_engines_importable()
    from engines.base import EngineRequest

    return EngineRequest


# --------------------------------------------------------------------------
# 2. traducao manifesto -> EngineRequest
# --------------------------------------------------------------------------
def _slug(texto: str) -> str:
    """Normaliza um rotulo para usar em nome de arquivo/diretorio."""
    limpo = _NOME_SEGURO.sub("_", str(texto)).strip("_")
    if not limpo:
        raise ValueError(f"rotulo sem nenhum caractere aproveitavel: {texto!r}")
    return limpo


def _posix(caminho: "str | Path | None") -> "str | None":
    """Caminho com barra normal (o .cfg interpola cru; '\\d' quebraria)."""
    if caminho is None:
        return None
    return Path(caminho).as_posix()


def run_dir_name(manifest: PairManifest) -> str:
    """Nome do `run_dir` que o motor deriva de target/ligand (`run-<a>-<b>`)."""
    return f"run-{_slug(manifest.bait)}-{_slug(manifest.partner)}"


def expected_run_dir(manifest: PairManifest, workdir: "str | Path") -> Path:
    """Diretorio onde o haddock3 VAI escrever (so passa a existir apos `run`)."""
    return Path(workdir) / run_dir_name(manifest)


def _segids_do_pdb(caminho: "str | Path") -> "set[str]":
    """Segids (colunas 73-76) presentes nas linhas ATOM/HETATM de um PDB."""
    achados: "set[str]" = set()
    for linha in Path(caminho).read_text(encoding="utf-8", errors="replace").splitlines():
        if linha.startswith(("ATOM  ", "HETATM")):
            achados.add(linha[72:76].strip())
    return achados


def _conferir_segid(caminho: str, esperado: str, papel: str, pair_id: str) -> None:
    """Falha alto se o PDB nao tiver o segid que o ``ambig.tbl`` referencia.

    ``build_ambig_tbl`` escreve ``resid N and segid A`` (receptor) e
    ``... segid C`` (parceiro) FIXOS. Um PDB com outro segid faz cada restricao
    selecionar ZERO atomos: o CNS roda sem AIR daquele lado e devolve uma pose
    plausivel e errada. Conferir aqui e barato; descobrir depois, nao.
    """
    achados = _segids_do_pdb(caminho)
    if achados == {esperado}:
        return
    raise ValueError(
        f"{pair_id}: o PDB do {papel} ({caminho}) tem segid {sorted(achados)}, "
        f"mas o ambig.tbl do motor referencia 'segid {esperado}'. Gere os PDBs com "
        "funnel.split.split_complex (que ja grava A/C) ou renomeie cadeia+segid "
        "antes de rodar - com segid divergente o HADDOCK3 roda SEM restricao."
    )


def _exigir(valor: "str | None", campo: str, pair_id: str) -> str:
    """Campo de caminho obrigatorio: ausente = erro alto, nunca chute."""
    if not valor:
        raise ValueError(
            f"{pair_id}: manifesto sem '{campo}' -- rode o estagio de split "
            "(CIF->PDB) antes do HADDOCK3"
        )
    caminho = Path(valor)
    if not caminho.is_file():
        raise FileNotFoundError(f"{pair_id}: '{campo}' nao existe em disco: {caminho}")
    return caminho.as_posix()


def _conferir_referencia_bate_com_os_modelos(
    info: "Dict[str, Any]", receptor: str, ligante: str, pair_id: str
) -> None:
    """Confere que a cadeia ``A``/``C`` da referencia e a MESMA molecula do modelo.

    O ``receptor_chain`` do caprieval (default ``"A"``) e resolvido sobre a
    nomenclatura da REFERENCIA. Se a referencia tivesse sido gerada com papeis
    diferentes dos PDBs de entrada (posicional x declarado -- no DEMO01 a
    diferenca e barnase 376 res x barstar 760 res), o caprieval reportaria
    L-RMSD/I-L-RMSD com receptor e ligante TROCADOS: numero plausivel e errado.
    Referencia e PDBs saem do mesmo `.cif` e do mesmo modelo, entao os numeros de
    residuo tem de bater exatamente.
    """
    from funnel import split as _split

    ref = {c: set(v) for c, v in info["residues"].items()}
    do_receptor = {n for v in _split.residues_by_chain(receptor).values() for n in v}
    do_ligante = {n for v in _split.residues_by_chain(ligante).values() for n in v}
    for cadeia, do_modelo, papel, arquivo in (
        (RECEPTOR_SEGID, do_receptor, "receptor/isca", receptor),
        (PARTNER_SEGID, do_ligante, "parceiro/ligante", ligante),
    ):
        if ref.get(cadeia) == do_modelo:
            continue
        raise ValueError(
            f"{pair_id}: a cadeia {cadeia!r} da referencia do caprieval "
            f"({info['path']}, {len(ref.get(cadeia, ()))} residuos) nao bate com o "
            f"PDB do {papel} ({arquivo}, {len(do_modelo)} residuos). Referencia e "
            "PDBs precisam sair do MESMO .cif com os MESMOS papeis -- caso "
            "contrario o caprieval reporta L-RMSD/DockQ com receptor e ligante "
            "trocados. Regere os dois com os papeis declarados da campanha "
            "(funnel.haddock_adapter.ensure_split(..., chain_roles=...))."
        )


def ensure_reference(
    manifest: PairManifest,
    out_dir: "str | Path",
    *,
    chain_roles: "Dict[str, Any] | None" = None,
) -> str:
    """Garante o PDB de referencia do caprieval do par (o complexo A+C).

    Idempotente: se ``manifest.reference_pdb`` ja aponta para um arquivo valido,
    ele e CONFERIDO (`funnel.split.check_reference_pdb`) e reaproveitado. Caso
    contrario a referencia e gerada do ``cif_model`` em
    ``<out_dir>/reference.pdb`` e gravada no manifesto.

    **Nunca** devolve o `.cif` como alternativa: o haddock3 le a referencia como
    PDB de colunas fixas e um mmCIF derruba o modulo caprieval inteiro (ver o
    cabecalho deste modulo). Falta de ``cif_model``, arquivo sumido ou referencia
    invalida viram ``ValueError``/``FileNotFoundError``.
    """
    from funnel import split as _split

    if manifest.reference_pdb:
        caminho = Path(manifest.reference_pdb)
        if not caminho.is_file():
            raise FileNotFoundError(
                f"{manifest.pair_id}: 'reference_pdb' aponta para um arquivo que nao "
                f"existe: {caminho}. Limpe o campo para regerar a referencia do "
                "caprieval a partir do cif_model."
            )
        _split.check_reference_pdb(caminho)  # conferido, nunca assumido
        return str(caminho)

    if not manifest.cif_model:
        raise ValueError(
            f"{manifest.pair_id}: sem 'cif_model' nao da para gerar a referencia do "
            "caprieval -- e sem referencia o DockQ do capri_clt sai "
            "auto-referenciado (dockq=1.0 por construcao), inutil para o GATE 2"
        )

    papeis = _interface_chain_roles(manifest.cif_model, chain_roles)
    caminho = _split.reference_pdb(
        manifest.cif_model,
        out_dir,
        REFERENCE_NAME,
        receptor_chains=papeis["bait_chains"],
        ligand_chain=papeis["partner_chain"],
    )
    manifest.reference_pdb = caminho
    return caminho


def _interface_chain_roles(
    cif_model: str, chain_roles: "Dict[str, Any] | None"
) -> "Dict[str, Any]":
    """`funnel.interface.chain_roles` com import tardio (evita ciclo de import)."""
    from funnel import interface as _interface

    return _interface.chain_roles(cif_model, esperado=chain_roles)


def ensure_split(
    manifest: PairManifest,
    out_dir: "str | Path",
    *,
    chain_roles: "Dict[str, Any] | None" = None,
) -> "Dict[str, Any]":
    """Completa o manifesto com PDBs, residuos ativos e REFERENCIA do par.

    E aqui que os papeis DECLARADOS pela campanha (bloco ``chain_roles`` do
    ``config/subproject.*.yaml``) chegam ao split e a interface - sem isso, os
    dois cairiam no fallback POSICIONAL do motor e, no DEMO01, o
    ``receptor.pdb`` sairia com a BARSTAR embora ``manifest.bait`` diga
    ``barnase``. Com ``chain_roles``, ``receptor_pdb``/``active_residues``
    passam a ser os do RECEPTOR declarado (barnase, cadeia B de origem) e
    ``ligand_pdb``/``partner_active_residues`` os do LIGANTE declarado (barstar,
    cadeia A de origem); os PDBs continuam gravados com cadeia/segid ``A``
    (receptor) e ``C`` (ligante), que e o que o ``ambig.tbl`` referencia.

    Alem dos dois PDBs de entrada, sai daqui tambem o ``reference_pdb`` -- o
    complexo INTEIRO num unico PDB (cadeias ``A``+``C``), que e a referencia do
    caprieval. Ele nasce do mesmo ``cif_model`` e dos MESMOS papeis do split, de
    modo que a cadeia ``A`` da referencia e sempre a mesma molecula do
    ``receptor.pdb``.

    Nao refaz trabalho: campos ja preenchidos no manifesto sao respeitados
    (idempotente). Devolve o que foi feito::

        {"split": bool, "interface": bool, "reference": bool,
         "papeis_fonte": "declarado", ...}

    Levanta ``ValueError``/``FileNotFoundError`` se nao houver ``cif_model``
    para derivar nada, ou se os papeis declarados nao baterem com o modelo
    (``fail_if_ambiguous``).
    """
    from funnel import interface as _interface
    from funnel import split as _split

    precisa_pdb = not (manifest.receptor_pdb and manifest.ligand_pdb)
    precisa_iface = not (manifest.active_residues and manifest.partner_active_residues)
    precisa_ref = not manifest.reference_pdb
    relatorio: "Dict[str, Any]" = {
        "split": False, "interface": False, "reference": False
    }
    if not (precisa_pdb or precisa_iface or precisa_ref):
        relatorio["papeis_fonte"] = "ja preenchido no manifesto"
        return relatorio

    if not manifest.cif_model:
        raise ValueError(
            f"{manifest.pair_id}: manifesto sem 'cif_model' - sem o complexo do "
            "Boltz nao da para gerar os PDBs nem os residuos ativos"
        )

    papeis = _interface.chain_roles(manifest.cif_model, esperado=chain_roles)
    relatorio["papeis_fonte"] = papeis["fonte"]
    relatorio["receptor_chains_origem"] = list(papeis["bait_chains"])
    relatorio["ligand_chain_origem"] = papeis["partner_chain"]

    if precisa_pdb:
        saida = _split.split_complex(
            manifest.cif_model,
            out_dir,
            receptor_chains=papeis["bait_chains"],
            ligand_chain=papeis["partner_chain"],
        )
        manifest.receptor_pdb = saida["receptor_pdb"]
        manifest.ligand_pdb = saida["ligand_pdb"]
        relatorio["split"] = True

    if precisa_iface:
        res = _interface.iface_residues(
            manifest.cif_model,
            bait_chains=papeis["bait_chains"],
            partner_chain=papeis["partner_chain"],
        )
        manifest.active_residues = list(res["bait_residues"])
        manifest.partner_active_residues = list(res["partner_residues"])
        relatorio["interface"] = True
        relatorio["n_pairs"] = res["n_pairs"]

    if precisa_ref:
        # MESMOS papeis ja resolvidos acima: a cadeia 'A' da referencia e a mesma
        # molecula do receptor.pdb (no DEMO01, a barnase declarada).
        manifest.reference_pdb = _split.reference_pdb(
            manifest.cif_model,
            out_dir,
            REFERENCE_NAME,
            receptor_chains=papeis["bait_chains"],
            ligand_chain=papeis["partner_chain"],
        )
        relatorio["reference"] = True

    return relatorio


def build_request(
    manifest: PairManifest,
    workdir: "str | Path",
    *,
    protocol_params: "Dict[str, Any] | None" = None,
    dry_run: bool = False,
) -> "EngineRequest":
    """Monta o `EngineRequest` do HADDOCK3 a partir do manifesto.

    Mapeamento (fixo, nao inferido em runtime):

    ==========================  =========================================
    EngineRequest               origem no PairManifest
    ==========================  =========================================
    ``campaign``                ``manifest.campaign``
    ``target``                  ``manifest.bait``   (receptor, segid A)
    ``ligand``                  ``manifest.partner`` (parceiro, segid C)
    ``kind``                    ``"docking"`` (fixo)
    ``inputs["receptor_pdb"]``  ``manifest.receptor_pdb``
    ``inputs["ligand_pdb"]``    ``manifest.ligand_pdb``
    ``params["active_residues"]``          ``manifest.active_residues``
    ``params["partner_active_residues"]``  ``manifest.partner_active_residues``
    ``params["protein_protein"]``          ``True`` (fixo -- PPI)
    ``params["caprieval_reference"]``      ``manifest.reference_pdb``
    ==========================  =========================================

    ``protein_protein=True`` faz o motor **omitir** ``ligand_param_fname`` /
    ``ligand_top_fname`` no bloco ``[topoaa]``: essas linhas so existem para
    ligante pequeno (residuo nao-padrao que precisa de topologia CNS propria);
    duas cadeias de aminoacido padrao o HADDOCK3 resolve sozinho.

    ``caprieval_reference`` e o que faz o ``capri_clt.tsv`` sair com **DockQ/
    iRMSD contra a referencia** em vez de auto-referenciado (cluster rank 1 com
    ``dockq=1.0`` por construcao). Sem isso o numero e inutil para o GATE 2 --
    ver o cabecalho deste modulo.

    Ele aponta para ``manifest.reference_pdb`` -- o complexo convertido para PDB
    (cadeias ``A``+``C``), **nunca** para o ``.cif``: o haddock3 fatia colunas de
    PDB em qualquer referencia e um mmCIF quebra o caprieval inteiro. Se o campo
    ainda estiver vazio, a referencia e gerada aqui em ``<workdir>/pdbs`` a
    partir do ``cif_model`` (e gravada no manifesto); se nao der para gerar ou
    conferir, o erro sobe -- sem cair de volta no `.cif` e sem omitir a
    referencia. Alem disso, as cadeias ``A``/``C`` da referencia sao conferidas
    contra os PDBs de entrada (mesmos numeros de residuo), o que impede um
    caprieval com receptor e ligante trocados.

    ``protocol_params`` (opcional) e repassado como ``req.protocol["params"]``,
    que e de onde `build_cfg` le ``modules``/``sampling_rigidbody``/
    ``select_rigidbody``/``select_flexref``. Vazio = defaults do motor
    (``topoaa, rigidbody, caprieval, flexref, emref, caprieval``).

    Levanta ``ValueError``/``FileNotFoundError`` se faltar PDB, se os PDBs nao
    existirem em disco ou se nao houver residuo ativo da isca (sem AIR o
    HADDOCK3 nao tem o que guiar).
    """
    if not isinstance(manifest, PairManifest):
        raise TypeError(
            f"build_request: esperava PairManifest, veio {type(manifest).__name__}"
        )
    if not manifest.active_residues:
        raise ValueError(
            f"{manifest.pair_id}: 'active_residues' vazio -- sem residuos ativos da "
            "isca nao ha AIR e o HADDOCK3 nao tem restricao para guiar o docking"
        )
    if not (manifest.partner_active_residues
            or getattr(manifest, "partner_passive_residues", None)):
        raise ValueError(
            f"{manifest.pair_id}: o lado do parceiro esta vazio -- declare "
            "'partner_active_residues' (AIR ativo-ativo) ou "
            "'partner_passive_residues' (parceiro passivo, quando nao ha homologo "
            "confiavel para projetar a interface dele)"
        )
    if not (manifest.cif_model or manifest.reference_pdb):
        raise ValueError(
            f"{manifest.pair_id}: 'cif_model' ausente -- dele sai a referencia do "
            "caprieval; sem referencia o DockQ do capri_clt sai auto-referenciado "
            "(inutil)"
        )

    receptor = _exigir(manifest.receptor_pdb, "receptor_pdb", manifest.pair_id)
    ligante = _exigir(manifest.ligand_pdb, "ligand_pdb", manifest.pair_id)

    # contrato com o ambig.tbl (segids fixos do motor) - conferido, nao assumido
    _conferir_segid(receptor, RECEPTOR_SEGID, "receptor/isca", manifest.pair_id)
    _conferir_segid(ligante, PARTNER_SEGID, "parceiro/ligante", manifest.pair_id)

    wd = Path(workdir)

    # referencia do caprieval: SEMPRE o PDB do complexo, nunca o .cif (ver
    # docstring). Gera se faltar, confere sempre, e cruza com os PDBs de entrada.
    from funnel import split as _split

    referencia = ensure_reference(manifest, wd / "pdbs")
    info = _split.check_reference_pdb(referencia)
    _conferir_referencia_bate_com_os_modelos(
        info, receptor, ligante, manifest.pair_id
    )
    referencia = Path(referencia).as_posix()

    EngineRequest = _request_cls()
    return EngineRequest(
        campaign=manifest.campaign,
        target=_slug(manifest.bait),
        ligand=_slug(manifest.partner),
        kind="docking",
        protocol={"params": dict(protocol_params or {})},
        params={
            "protein_protein": True,
            "active_residues": list(manifest.active_residues),
            "partner_active_residues": list(manifest.partner_active_residues),
            # PASSIVOS: opcionais; entram SO como alvo possivel no ambig.tbl e nao
            # geram `assign` proprio. Sem essa distincao, uma lista grande de
            # residuos de superficie vira restricao ATIVA impossivel de
            # satisfazer: a energia de AIR sobe ordens de grandeza, o score do
            # HADDOCK fica positivo, e o fnat perto de zero que sai no fim e
            # artefato da restricao impossivel -- nao resultado sobre o par.
            # Ver `funnel.air_homolog.com_parceiro_passivo`.
            "passive_residues": list(getattr(manifest, "passive_residues", []) or []),
            "partner_passive_residues": list(
                getattr(manifest, "partner_passive_residues", []) or []),
            "caprieval_reference": referencia,
        },
        inputs={"receptor_pdb": receptor, "ligand_pdb": ligante},
        workdir=wd,
        dry_run=bool(dry_run),
    )


# --------------------------------------------------------------------------
# 3. prepare - escreve .cfg + ambig.tbl (NAO precisa do binario haddock3)
# --------------------------------------------------------------------------
def prepare(
    manifest: PairManifest,
    workdir: "str | Path",
    *,
    protocol_params: "Dict[str, Any] | None" = None,
    chain_roles: "Dict[str, Any] | None" = None,
) -> HaddockArtifacts:
    """Gera `.cfg` e `ambig.tbl` no `workdir` via `Haddock3Engine.prepare`.

    ``chain_roles`` = bloco declarado pela campanha. Ele so e usado quando o
    manifesto ainda NAO traz os PDBs/residuos ativos: nesse caso `ensure_split`
    os deriva do ``cif_model`` com os papeis DECLARADOS (receptor = barnase no
    DEMO01), em vez do fallback posicional. Manifesto ja completo passa direto.

    E **logica pura**: roda com o binario `haddock3` ausente. Retorna
    `HaddockArtifacts` com ``cfg`` e ``ambig_tbl`` preenchidos; ``run_dir``
    fica ``None`` de proposito (o diretorio so nasce quando o HADDOCK3 roda --
    use `expected_run_dir` se precisar do caminho previsto).

    Levanta ``RuntimeError`` se o motor nao gravar os arquivos (ex.: contagem de
    AIRs zerada) -- nunca devolve um artefato "plausivel" que nao existe.
    """
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    if chain_roles is not None:
        # opt-in: so derivamos algo quando a campanha DECLARA os papeis. Sem
        # declaracao, manifesto incompleto continua falhando alto em
        # `build_request` (nunca preenchido pelo fallback posicional, que no
        # DEMO01 poria a barstar no receptor.pdb).
        ensure_split(manifest, wd / "pdbs", chain_roles=chain_roles)
    req = build_request(
        manifest, wd, protocol_params=protocol_params, dry_run=False
    )
    req = _engine().prepare(req)

    n_air = req.outputs.get("_n_restraints", 0)
    if not n_air:
        raise RuntimeError(
            f"{manifest.pair_id}: Haddock3Engine.prepare produziu 0 restricoes "
            "(ambig.tbl vazio) -- confira 'active_residues' no manifesto"
        )
    cfg = req.outputs.get("cfg")
    tbl = req.outputs.get("ambig_tbl")
    if not cfg or not tbl:
        raise RuntimeError(
            f"{manifest.pair_id}: Haddock3Engine.prepare nao gravou cfg/ambig.tbl "
            f"em {wd} (workdir invalido ou dry_run ligado indevidamente)"
        )
    return HaddockArtifacts(cfg=str(cfg), ambig_tbl=str(tbl))


# --------------------------------------------------------------------------
# 4. run - exige o binario; falha alto e preserva o que ja foi gerado
# --------------------------------------------------------------------------
def _resultado_docker(saida, artefatos, n_air) -> "EngineResult":
    """Converte o `DockerRunResult` no mesmo `EngineResult` do motor local.

    O chamador (ponte, pipeline, n8n) nao precisa saber ONDE o HADDOCK3 rodou:
    o envelope de retorno e identico nos dois caminhos.
    """
    _ensure_engines_importable()
    from engines.base import EngineResult

    return EngineResult(
        engine="haddock3",
        status=saida.status,
        cmdline=list(saida.cmdline or []),
        outputs={"cfg": artefatos.cfg, "ambig_tbl": artefatos.ambig_tbl,
                 "cfg_container": saida.cfg_container, "prepared": True},
        metrics={"n_restraints": n_air, "motor": "docker",
                 "returncode": saida.returncode},
        log=saida.log or "",
        error=saida.error,
    )


def run(
    manifest: PairManifest,
    workdir: "str | Path",
    *,
    protocol_params: "Dict[str, Any] | None" = None,
    chain_roles: "Dict[str, Any] | None" = None,
    dry_run: bool = False,
    motor: str = "auto",
) -> "EngineResult":
    """Dispara `haddock3 <cfg>` -- no binario local OU num container Linux.

    ``motor``:

    * ``"local"``  -- so `Haddock3Engine.run` (exige o binario `haddock3` no PATH);
    * ``"docker"`` -- so `funnel.haddock_docker` (imagem `ppi-haddock3`);
    * ``"auto"`` (default) -- usa o local se existir, senao o Docker se a imagem
      existir, senao devolve erro citando **os dois** caminhos e como habilitar
      cada um. Nunca inventa um sucesso.

    O motor de calculo do HADDOCK3 e o CNS, que nao tem build para Windows -- por
    isso, no Windows, o caminho normal e o Docker. Ver `funnel/haddock_docker.py`.

    **Nada e mascarado.** Se o binario `haddock3` nao estiver instalado (o caso
    desta maquina hoje), o motor devolve ``EngineResult(status="error", ...)``
    e esse resultado e propagado como veio. O adaptador so ENRIQUECE o retorno,
    nunca o converte em sucesso:

    * ``result.outputs["cfg"]`` / ``["ambig_tbl"]`` -- caminhos dos arquivos que
      **ja foram gerados** e ficam prontos para rodar depois (o `prepare` roda
      antes e e independente do binario);
    * ``result.outputs["prepared"] = True`` -- sinaliza ao chamador que o par
      esta no estagio "prepared", nao "docked";
    * ``result.metrics["n_restraints"]`` -- quantas AIRs entraram no `.tbl`;
    * ``result.error`` ganha o sufixo indicando onde estao os arquivos.

    Ou seja: erro continua sendo erro (``result.ok`` e ``False``), mas o
    chamador sabe que so falta o binario -- e nao precisa refazer o preparo.

    ``dry_run=True`` devolve ``status="ok"`` com apenas o ``cmdline``, sem
    executar nada.
    """
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)

    # prepare primeiro: garante cfg/tbl em disco mesmo que o run falhe depois.
    # (e e ele quem aplica os papeis declarados via `chain_roles`)
    artefatos = prepare(
        manifest, wd, protocol_params=protocol_params, chain_roles=chain_roles
    )

    if motor not in {"auto", "local", "docker"}:
        raise ValueError(
            f"motor invalido: {motor!r} (use 'auto', 'local' ou 'docker')"
        )

    req = build_request(
        manifest, wd, protocol_params=protocol_params, dry_run=dry_run
    )
    n_air = req.outputs.get("_n_restraints", 0)

    # ---- escolha do motor -------------------------------------------------
    from . import haddock_docker as _hd

    local_ok = bool(check_toolchain().get("available"))
    imagem = _hd.check_image()
    docker_ok = bool(imagem.get("available"))

    escolhido = motor
    if motor == "auto":
        if local_ok:
            escolhido = "local"
        elif docker_ok or dry_run:
            escolhido = "docker"
        else:
            _ensure_engines_importable()
            from engines.base import EngineResult

            return EngineResult(
                engine="haddock3", status="error",
                outputs={"cfg": artefatos.cfg, "ambig_tbl": artefatos.ambig_tbl,
                         "prepared": True},
                metrics={"n_restraints": n_air},
                error=(
                    "nenhum motor HADDOCK3 disponivel. "
                    "(a) binario local: ausente -- e o CNS nao tem build para "
                    "Windows, entao instalar no env conda daqui nao resolve; "
                    f"(b) container: {imagem.get('error')}. "
                    f"O cfg e o ambig.tbl JA estao prontos em {artefatos.cfg} / "
                    f"{artefatos.ambig_tbl} -- basta habilitar um dos motores."
                ),
            )

    if escolhido == "docker":
        saida = _hd.run_haddock3(artefatos.cfg, dry_run=dry_run)
        return _resultado_docker(saida, artefatos, n_air)

    result = _engine().run(req)

    result.outputs.setdefault("cfg", artefatos.cfg)
    result.outputs.setdefault("ambig_tbl", artefatos.ambig_tbl)
    result.outputs["prepared"] = True
    result.metrics.setdefault("n_restraints", req.outputs.get("_n_restraints", 0))
    if result.status != "ok" and result.error:
        result.error = (
            f"{result.error} | cfg e ambig.tbl JA foram gerados e estao prontos "
            f"para rodar depois: {artefatos.cfg} / {artefatos.ambig_tbl}"
        )
    return result


# --------------------------------------------------------------------------
# 5. toolchain
# --------------------------------------------------------------------------
def check_toolchain() -> "Dict[str, Any]":
    """Repassa `Haddock3Engine.check_toolchain()` sem interpretar.

    Formato do motor: ``{"available": bool, "tools": {"haddock3": {...}}}``.
    Sem o binario instalado vem ``{"available": False, ...}`` -- e nao e erro:
    `prepare` funciona assim mesmo, so `run` depende dele.
    """
    return _engine().check_toolchain()
