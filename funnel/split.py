# -*- coding: utf-8 -*-
"""Estagio "split": quebra o complexo do Boltz-2 em dois PDBs para o HADDOCK3.

Posicao no funil:

    Boltz-2 -> score -> GATE 1 -> interface -> [SPLIT] -> HADDOCK3 -> GATE 2

O HADDOCK3 nao aceita o complexo pronto: ele precisa dos dois parceiros como
arquivos PDB SEPARADOS (receptor e ligante), que ele reposiciona um em relacao
ao outro guiado pelo `ambig.tbl`. Este modulo produz esses dois arquivos a partir
do MESMO `.cif` de onde sairam os residuos ativos (`funnel.interface`), de modo
que a numeracao de residuo do `ambig.tbl` e a dos PDBs sao necessariamente a
mesma.

Armadilha que este modulo existe para impedir
---------------------------------------------
`Bio.PDB.PDBParser` aceita um arquivo `.cif` sem reclamar e devolve uma estrutura
com ZERO atomos - em silencio. Um pipeline que caia nisso escreve PDBs vazios,
o HADDOCK3 roda, e sai um numero plausivel e completamente errado. Por isso aqui:

1. a leitura e SEMPRE `MMCIFParser` (via `scoring_engine._common.parse_model`), nunca
   `PDBParser`;
2. depois de escrever cada PDB o arquivo e REABERTO e o numero de atomos e
   conferido - zero atomos vira ``RuntimeError``, nao um arquivo vazio no disco.

Cofatores (HETATM)
------------------
`keep_hetero=False` por DEFAULT: ATP, MG e agua sao DESCARTADOS. Isso e uma
decisao explicita, nao um esquecimento - o HADDOCK3/CNS so consegue tratar um
cofator se existir topologia e parametros CNS para ele (`toppar/ligand.top` e
`ligand.param`); sem isso o run falha ou, pior, ignora o cofator silenciosamente.
No DEMO01 barnase o ATP e o Mg2+ ocupam o bolso da barnase e sao estruturalmente
relevantes: se um dia forem necessarios, o caminho e gerar a topologia CNS
(prodrg/pdb2ligand) e so entao ligar `keep_hetero=True`.

Com `keep_hetero=True` sao mantidos os HETATM que estao DENTRO das cadeias
selecionadas. Cadeias formadas SO por hetero (no DEMO01, C=ATP e D=MG sao cadeias
proprias) nao pertencem nem ao receptor nem ao ligante e continuam de fora - elas
sao listadas no retorno em ``dropped_hetero_chains``, para o descarte ficar
visivel e nunca silencioso.

Segids A/C (CONTRATO COM O `ambig.tbl`)
---------------------------------------
`engines.haddock3.build_ambig_tbl` escreve os segids **fixos** ``A`` (receptor) e
``C`` (parceiro). Se os PDBs sairem com as cadeias originais do Boltz (no DEMO01,
A e B), TODA restricao do lado do parceiro (``resid N and segid C``) seleciona
ZERO atomos: o HADDOCK3 roda sem AIR daquele lado e devolve uma pose plausivel e
errada - exatamente o que este projeto proibe. Por isso o split **renomeia**
cadeia e segid para ``A``/``C`` por padrao (``receptor_chain_out``/
``ligand_chain_out``); passe ``None`` nos dois so para inspecao.

Papeis explicitos
-----------------
A eleicao posicional (`funnel.interface.chain_roles`) e apenas o DEFAULT. Quando
a campanha declara os papeis (bloco ``chain_roles`` de
``config/subproject.*.yaml``: no DEMO01, receptor=barnase cadeia B, ligante=barstar
cadeia A), passe ``receptor_chains``/``ligand_chain`` explicitamente - a posicao
no arquivo NAO e identidade biologica.

Referencia do caprieval (`reference_pdb`)
-----------------------------------------
O mesmo `.cif` precisa virar tambem a REFERENCIA do modulo `caprieval` do
HADDOCK3 - e ela **nao pode ser o `.cif`**. Diagnostico feito na propria imagem
`ppi-haddock3` (haddock3 2026.7.0): o haddock3 le qualquer referencia como PDB de
COLUNAS FIXAS. `libs/libpdb.py` define ``slc_resseq = slice(22, 26)`` e
`libs/libalign.py:884` (`pdb2fastadic`) faz ``int(line[slc_resseq])``. Num mmCIF
do Boltz a primeira linha e::

    ATOM 1 N N . MET 1 1 ? A -2.54935 -3.07189 -43.86617 1 1 A MET 52.866 1

e a fatia ``[22:26]`` vale ``' A -'`` -> ``ValueError: invalid literal for int()
with base 10: ' A -'``. Pior: o unico guard do haddock3
(`libpdb.handle_input_reference`) so exige que a linha COMECE com ``ATOM``/
``HETATM``, o que um mmCIF satisfaz - ele aceita o arquivo e so quebra la dentro,
1 vez por modelo, dentro dos workers do `libparallel`, que engolem a excecao e
devolvem ``None``; o sintoma final e um ``AttributeError: 'NoneType' object has
no attribute 'identificator'`` 200 linhas adiante. Nao existe UMA linha com
"cif" em `libpdb`/`libalign`/`libcapri`/`caprieval` - mmCIF nao e suportado,
ponto.

Por isso `reference_pdb` grava o COMPLEXO INTEIRO num unico PDB com as MESMAS
cadeias/segids dos modelos que o HADDOCK3 produz (receptor=``A``, ligante=``C``):

* **IDs de cadeia:** o `align_seq` do haddock3 casaria cadeias de nomes
  diferentes por identidade de sequencia, mas ``receptor_chain`` (default ``A``)
  e resolvido sobre a nomenclatura da REFERENCIA (`libcapri.py:267-277`). Manter
  o naming do Boltz (DEMO01: A=barstar, B=barnase) faria o caprieval tratar a
  BARSTAR como receptor e reportar L-RMSD/I-L-RMSD com os papeis trocados -
  numero plausivel e errado.
* **Cofatores:** ATP/MG ficam de fora (`keep_hetatm` do caprieval e ``false``, e
  no DEMO01 o ATP e justamente a cadeia ``C``, que colidiria com o ligante).
* **Numeracao:** nao precisa casar com a do modelo (o alinhamento reconstroi o
  mapa), mas precisa ser um inteiro na fatia 22:26 - conferido em
  `check_reference_pdb`.
* **1 modelo so:** varios ``MODEL`` viram varias referencias
  (`libpdb.handle_input_reference`), o que nao e o que queremos.
* **segid e irrelevante** para o caprieval (nenhuma funcao de `libalign`/
  `libcapri` usa ``slc_segid``); gravamos assim mesmo, por coerencia com o
  ``ambig.tbl``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

from . import interface as _interface

__all__ = [
    "split_complex",
    "reference_pdb",
    "check_reference_pdb",
    "residues_by_chain",
]


# --------------------------------------------------------------------------
# helpers internos
# --------------------------------------------------------------------------
def _select_class(chain_ids: "Sequence[str]", keep_hetero: bool):
    """Cria um `Bio.PDB.Select` que aceita so as cadeias pedidas.

    Import lazy de biopython (o modulo precisa importar sem biopython instalado).
    """
    from Bio.PDB import Select

    alvos = set(chain_ids)

    class _ChainSelect(Select):
        """Filtro de cadeia (+ opcionalmente de HETATM) para o PDBIO."""

        def accept_chain(self, chain):  # noqa: D102 - contrato do biopython
            return 1 if chain.id in alvos else 0

        def accept_residue(self, residue):  # noqa: D102
            if residue.id[0] == " ":
                return 1
            return 1 if keep_hetero else 0

    return _ChainSelect()


def _count_hetero(model, chain_ids: "Sequence[str]") -> int:
    """Quantos residuos HETERO existem dentro das cadeias indicadas."""
    alvos = set(chain_ids)
    return sum(
        1
        for c in model
        if c.id in alvos
        for r in c
        if r.id[0] != " "
    )


def _check_pdb_writable(model, chain_ids: "Sequence[str]") -> None:
    """Confere que as cadeias cabem no formato PDB (colunas fixas).

    O PDB tem 1 caractere para a cadeia e 4 digitos para o numero do residuo.
    Se o modelo estourar isso, o arquivo sai corrompido de forma sutil - melhor
    falhar aqui, com mensagem clara.
    """
    alvos = set(chain_ids)
    for c in model:
        if c.id not in alvos:
            continue
        if len(str(c.id)) != 1:
            raise ValueError(
                f"cadeia {c.id!r} tem mais de 1 caractere e nao cabe no formato "
                "PDB (coluna 22). Renomeie as cadeias antes do split."
            )
        for r in c:
            if abs(int(r.id[1])) > 9999:
                raise ValueError(
                    f"cadeia {c.id}: residuo {r.id[1]} excede 4 digitos e nao "
                    "cabe no formato PDB (colunas 23-26)."
                )


def _renomear(model, de: str, para: str) -> None:
    """Renomeia UMA cadeia (id + segid de todos os atomos) dentro do modelo.

    O segid e o que o `ambig.tbl` do motor referencia (``segid A``/``segid C``);
    a cadeia e o que o PDB grava na coluna 22. Os dois sao ajustados juntos para
    o `.tbl` e o `.pdb` nao poderem divergir.
    """
    if de == para:
        cadeia = model[de]
    else:
        if para in [c.id for c in model]:
            raise ValueError(
                f"nao da para renomear a cadeia {de!r} para {para!r}: ja existe "
                f"uma cadeia {para!r} no modelo. Renomeie-a antes (ou passe "
                "receptor_chain_out/ligand_chain_out=None)."
            )
        cadeia = model[de]
        cadeia.id = para
    # o segid gravado pelo PDBIO vem do RESIDUO (Bio.PDB.Residue.segid), nao do
    # atomo; sem isso as colunas 73-76 saem em branco e o 'segid A/C' do
    # ambig.tbl nao casa com nada.
    for residuo in cadeia:
        residuo.segid = f"{para:<4}"


def _conferir_cadeias_no_pdb(
    destino: Path, esperadas: "Sequence[str]", *, exigir_segid: bool = True
) -> None:
    """Confere no ARQUIVO escrito que cadeia e segid sao os esperados.

    Le as linhas ATOM/HETATM do PDB: coluna 22 (cadeia) e colunas 73-76 (segid).
    Divergencia = ``RuntimeError``, porque um segid errado faz o `ambig.tbl`
    selecionar zero atomos e o HADDOCK3 rodar sem restricao, em silencio.
    """
    alvo = {str(c).strip() for c in esperadas}
    cadeias: "set[str]" = set()
    segids: "set[str]" = set()
    for linha in destino.read_text(encoding="utf-8").splitlines():
        if linha.startswith(("ATOM  ", "HETATM")):
            cadeias.add(linha[21].strip())
            segids.add(linha[72:76].strip())
    if cadeias != alvo:
        raise RuntimeError(
            f"{destino}: cadeias gravadas {sorted(cadeias)} != esperadas {sorted(alvo)}"
        )
    if exigir_segid and segids != alvo:
        raise RuntimeError(
            f"{destino}: segid(s) gravado(s) {sorted(segids)} != {sorted(alvo)}. "
            "O ambig.tbl do motor referencia 'segid A'/'segid C'; com segid "
            "diferente a restricao seleciona ZERO atomos e o docking roda sem AIR."
        )


def _normalizar_colunas_pdb(destino: Path) -> None:
    """Põe o segID nas colunas 73-76 do PDB escrito (e o element em 77-78).

    POR QUE ISTO EXISTE: o `PDBIO` do biopython nem sempre alinha o campo segID
    com o formato PDB -- medido no 1.86, ele sai uma coluna a ESQUERDA (72-75).
    O CNS (motor do HADDOCK3) le o PDB por posicao de coluna FIXA, entao um
    segid fora de lugar e um segid inexistente para ele: o `ambig.tbl` do motor,
    que restringe por ``segid A``/``segid C``, selecionaria ZERO atomos e o
    docking rodaria SEM restricao, em silencio. E o bug que este modulo inteiro
    existe para impedir, entao a escrita e normalizada aqui em vez de depender
    da versao do biopython instalada.

    Idempotente: uma linha que ja tenha o segID em 73-76 passa intacta.
    """
    linhas = destino.read_text(encoding="utf-8").splitlines(keepends=True)
    saida: "list[str]" = []
    mexeu = False
    for linha in linhas:
        if not linha.startswith(("ATOM  ", "HETATM")):
            saida.append(linha)
            continue
        corpo = linha.rstrip("\n").rstrip("\r")
        fim = linha[len(corpo):]
        campos = corpo.ljust(80)
        if campos[72:76].strip():          # ja no lugar certo
            saida.append(linha)
            continue
        # o segid e o element podem estar deslocados: recupera o que houver
        # entre o b-factor (col. 67) e o fim, e reescreve nas posicoes certas.
        cauda = campos[66:80]
        pedacos = cauda.split()
        segid, element = "", ""
        if pedacos:
            # o ultimo pedaco de 1-2 letras e o element; o anterior, o segid
            if len(pedacos[-1]) <= 2:
                element = pedacos[-1]
                if len(pedacos) > 1:
                    segid = pedacos[-2]
            else:
                segid = pedacos[-1]
        if not segid:
            saida.append(linha)
            continue
        novo = (
            campos[:66].ljust(66)
            + " " * 6
            + f"{segid:<4.4s}"
            + f"{element:>2.2s}"
            + campos[78:80]
        )
        saida.append(novo.rstrip() + (fim or "\n"))
        mexeu = True
    if mexeu:
        destino.write_text("".join(saida), encoding="utf-8")


def _write_pdb(model, chain_ids: "Sequence[str]", destino: Path, keep_hetero: bool) -> int:
    """Escreve as cadeias indicadas num PDB e devolve o n. de atomos CONFERIDO.

    A conferencia e feita REABRINDO o arquivo escrito com `PDBParser`: se o
    resultado tiver zero atomos, levanta ``RuntimeError``. E esse o bug silencioso
    que o modulo existe para impedir.
    """
    from Bio.PDB import PDBIO, PDBParser

    _check_pdb_writable(model, chain_ids)

    destino.parent.mkdir(parents=True, exist_ok=True)
    io = PDBIO()
    io.set_structure(model)
    io.save(str(destino), select=_select_class(chain_ids, keep_hetero))

    if not destino.is_file():
        raise RuntimeError(f"PDBIO nao criou o arquivo esperado: {destino}")

    # o segID tem de cair nas colunas 73-76 para o CNS enxergar (ver a
    # docstring de _normalizar_colunas_pdb)
    _normalizar_colunas_pdb(destino)

    # re-leitura de verificacao (o PDB escrito agora E um PDB de verdade,
    # entao aqui o PDBParser e o parser CORRETO)
    conferido = PDBParser(QUIET=True).get_structure("chk", str(destino))
    n_atoms = sum(1 for _ in conferido.get_atoms())
    if n_atoms == 0:
        raise RuntimeError(
            f"PDB escrito com ZERO atomos: {destino} (cadeias pedidas: "
            f"{list(chain_ids)}, keep_hetero={keep_hetero}). Isso indica que a "
            "selecao de cadeias nao casou com o modelo ou que o .cif de origem "
            "esta vazio - o arquivo NAO deve seguir para o HADDOCK3."
        )
    return n_atoms


def _preparar_modelo(
    cif_path: "str | Path",
    *,
    receptor_chains: "Sequence[str] | None" = None,
    ligand_chain: "str | None" = None,
    esperado: "Mapping[str, Any] | None" = None,
    receptor_chain_out: "str | None" = "A",
    ligand_chain_out: "str | None" = "C",
) -> "Dict[str, Any]":
    """Le o `.cif`, elege os papeis e renomeia cadeia+segid para A/C.

    E o miolo COMPARTILHADO por `split_complex` (2 PDBs) e por `reference_pdb`
    (1 PDB com o complexo): os dois precisam eleger o receptor/ligante do mesmo
    jeito e aplicar a MESMA renomeacao, senao a referencia do caprieval sairia
    com papeis diferentes dos modelos - o erro plausivel-e-errado que o
    diagnostico do modulo descreve.

    Devolve ``{"caminho", "model", "papeis", "origem_receptor", "origem_ligante",
    "receptor_final", "ligante_final"}``.
    """
    from scoring_engine import _common

    caminho = _interface._check_cif(cif_path)
    # com `esperado`, os papeis vem da campanha e ja saem CONFERIDOS contra o
    # modelo (existencia + n_residues_esperado, honrando fail_if_ambiguous).
    papeis = _interface.chain_roles(caminho, esperado=esperado)
    origem_receptor: "List[str]" = (
        [str(c) for c in receptor_chains]
        if receptor_chains
        else list(papeis["bait_chains"])
    )
    origem_ligante: str = str(ligand_chain) if ligand_chain else papeis["partner_chain"]

    conhecidas = set(papeis["chain_sizes"])
    faltando = [c for c in [*origem_receptor, origem_ligante] if c not in conhecidas]
    if faltando:
        raise ValueError(
            f"{caminho.name}: cadeia(s) {faltando} nao existem (ou nao sao proteicas) "
            f"neste modelo; cadeias proteicas: {sorted(conhecidas)}"
        )
    if origem_ligante in origem_receptor:
        raise ValueError(
            f"{caminho.name}: a cadeia {origem_ligante!r} foi indicada como receptor "
            "E como ligante ao mesmo tempo"
        )
    if receptor_chain_out is not None and len(origem_receptor) > 1:
        raise ValueError(
            f"{caminho.name}: receptor com {len(origem_receptor)} cadeias "
            f"({origem_receptor}) nao cabe num unico segid {receptor_chain_out!r} sem "
            "colisao de numeracao. Una as cadeias antes ou passe "
            "receptor_chain_out=None (ciente de que o ambig.tbl do motor so "
            "referencia 'segid A'/'segid C')."
        )

    model = _common.parse_model(caminho)

    # renomeia via ids TEMPORARIOS para o destino (A/C) nunca colidir com uma
    # cadeia que ainda existe no modelo com esse mesmo id.
    receptor_final: "List[str]" = list(origem_receptor)
    ligante_final: str = origem_ligante
    if receptor_chain_out is not None or ligand_chain_out is not None:
        _POOL = "0123456789abcdefghijklmnopqrstuvwxyz"
        temporarios = [c for c in _POOL if c not in {ch.id for ch in model}]
        if len(temporarios) < 3:
            raise ValueError(
                f"{caminho.name}: sem id de cadeia livre para a renomeacao temporaria"
            )
        # Uma cadeia NAO selecionada pode ocupar o id de destino (no DEMO01, 'C' e
        # o ATP). Ela sai do caminho para um id livre - nao vai para o PDB de
        # qualquer forma, mas nao pode bloquear a renomeacao.
        selecionadas_origem = {*origem_receptor, origem_ligante}
        destinos = {
            str(x) for x in (receptor_chain_out, ligand_chain_out) if x is not None
        }
        livres = list(temporarios[2:])
        for cadeia in list(model):
            if cadeia.id in destinos and cadeia.id not in selecionadas_origem:
                cadeia.id = livres.pop()
        if receptor_chain_out is not None:
            _renomear(model, origem_receptor[0], temporarios[0])
            receptor_final = [str(receptor_chain_out)]
        if ligand_chain_out is not None:
            _renomear(model, origem_ligante, temporarios[1])
            ligante_final = str(ligand_chain_out)
        if receptor_chain_out is not None:
            _renomear(model, temporarios[0], str(receptor_chain_out))
        if ligand_chain_out is not None:
            _renomear(model, temporarios[1], str(ligand_chain_out))

    return {
        "caminho": caminho,
        "model": model,
        "papeis": papeis,
        "origem_receptor": origem_receptor,
        "origem_ligante": origem_ligante,
        "receptor_final": receptor_final,
        "ligante_final": ligante_final,
    }


# --------------------------------------------------------------------------
# API publica
# --------------------------------------------------------------------------
def residues_by_chain(pdb_path: "str | Path") -> "Dict[str, List[int]]":
    """Numeros de residuo (registros ATOM) por cadeia de um PDB de colunas fixas.

    Le como o proprio haddock3 le: cadeia na coluna 22 (``line[21]``) e numero na
    fatia ``line[22:26]``. Registros ``HETATM`` sao IGNORADOS - e o que o
    caprieval faz com ``keep_hetatm = false`` (default) em `pdb2fastadic` e
    `load_coords`. Serve para comparar a referencia com os PDBs de entrada sem
    reimplementar leitura de estrutura em dois lugares.

    Levanta ``ValueError`` se alguma linha ATOM tiver numero de residuo nao
    inteiro - que e exatamente o sintoma de ter passado um mmCIF disfarcado.
    """
    origem = Path(pdb_path)
    if not origem.is_file():
        raise FileNotFoundError(f"PDB nao encontrado: {origem}")
    achados: "Dict[str, List[int]]" = {}
    vistos: "Dict[str, set]" = {}
    for n, linha in enumerate(
        origem.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        # `startswith("ATOM")` (e nao "ATOM  ") de proposito: assim uma linha de
        # mmCIF (`ATOM 1 N N . MET ...`) tambem entra e explode no int() abaixo,
        # em vez de ser filtrada e o arquivo passar por "vazio". HETATM comeca
        # com 'HETATM' e continua de fora (keep_hetatm=false no caprieval).
        if not linha.startswith("ATOM"):
            continue
        cadeia = linha[21].strip()
        bruto = linha[22:26]
        try:
            numero = int(bruto)
        except ValueError as exc:
            raise ValueError(
                f"{origem}: linha {n} nao e um PDB de colunas fixas - a fatia "
                f"[22:26] (numero do residuo) vale {bruto!r}, nao um inteiro. "
                "Um arquivo mmCIF (.cif) lido como PDB produz exatamente isso, e "
                "e assim que o caprieval do HADDOCK3 quebra."
            ) from exc
        marcados = vistos.setdefault(cadeia, set())
        if numero not in marcados:
            marcados.add(numero)
            achados.setdefault(cadeia, []).append(numero)
    return {c: sorted(v) for c, v in achados.items()}


def check_reference_pdb(
    caminho: "str | Path",
    *,
    cadeias_esperadas: "Sequence[str]" = ("A", "C"),
) -> "Dict[str, Any]":
    """Confere que o arquivo serve como ``reference_fname`` do caprieval.

    O haddock3 NAO detecta formato: ele fatia colunas de PDB em qualquer arquivo
    que tenha linhas comecando com ``ATOM``/``HETATM`` - e um mmCIF passa nesse
    teste (a primeira coluna do loop ``_atom_site`` e ``group_PDB``). Este check
    e o guarda que transforma aquele modo de falha (10 warnings enterrados + um
    ``AttributeError`` incompreensivel) num erro acionavel ANTES de rodar.

    Confere: extensao ``.pdb``; pelo menos um registro ATOM; numero de residuo
    inteiro na fatia 22:26 em TODOS os ATOM; as cadeias presentes; e no maximo um
    registro ``MODEL`` (varios modelos viram varias referencias no haddock3).

    Devolve ``{"path", "n_atoms", "chains", "residues", "n_models"}``.
    """
    origem = Path(caminho)
    if origem.suffix.lower() != ".pdb":
        raise ValueError(
            f"referencia do caprieval precisa ser um PDB de colunas fixas, veio "
            f"{origem.suffix or '(sem extensao)'}: {origem}. O HADDOCK3 nao le "
            "mmCIF em lugar nenhum (nao ha parser de .cif em libpdb/libalign/"
            "libcapri): ele fatiaria as colunas do .cif e quebraria com "
            "\"invalid literal for int() with base 10: ' A -'\". Gere a "
            "referencia com funnel.split.reference_pdb."
        )
    if not origem.is_file():
        raise FileNotFoundError(f"referencia do caprieval nao existe: {origem}")

    texto = origem.read_text(encoding="utf-8", errors="replace")
    n_models = sum(1 for l in texto.splitlines() if l.startswith("MODEL "))
    if n_models > 1:
        raise ValueError(
            f"{origem}: {n_models} registros MODEL. O haddock3 "
            "(libpdb.handle_input_reference) quebraria isso em VARIAS referencias; "
            "grave um unico modelo."
        )
    n_atoms = sum(1 for l in texto.splitlines() if l.startswith("ATOM"))
    if n_atoms == 0:
        raise ValueError(
            f"{origem}: nenhum registro ATOM. Com keep_hetatm=false (default do "
            "caprieval) essa referencia nao tem NENHUM atomo utilizavel."
        )

    residuos = residues_by_chain(origem)  # valida a fatia 22:26 linha a linha
    alvo = {str(c).strip() for c in cadeias_esperadas}
    if set(residuos) != alvo:
        raise ValueError(
            f"{origem}: cadeias {sorted(residuos)} != esperadas {sorted(alvo)}. "
            "A referencia do caprieval tem de ter as MESMAS cadeias dos modelos do "
            "HADDOCK3 (receptor 'A', ligante 'C'): 'receptor_chain' e resolvido "
            "sobre a nomenclatura da REFERENCIA, entao um naming diferente troca "
            "os papeis e o L-RMSD/DockQ sai plausivel e errado."
        )
    return {
        "path": str(origem.resolve()),
        "n_atoms": n_atoms,
        "chains": {c: len(v) for c, v in residuos.items()},
        "residues": residuos,
        "n_models": n_models,
    }


def reference_pdb(
    cif_path: "str | Path",
    out_dir: "str | Path",
    name: str = "reference",
    *,
    esperado: "Mapping[str, Any] | None" = None,
    receptor_chains: "Sequence[str] | None" = None,
    ligand_chain: "str | None" = None,
    overwrite: bool = True,
) -> str:
    """Gera a REFERENCIA do caprieval: o complexo inteiro num unico PDB (A + C).

    Mesma origem (`.cif` do Boltz), mesma eleicao de papeis e mesma renomeacao do
    `split_complex` - so que aqui as duas cadeias vao para o MESMO arquivo, que e
    o que o ``reference_fname`` do bloco ``[caprieval]`` espera. Ver a secao
    "Referencia do caprieval" na docstring do modulo para o porque de cada
    exigencia (PDB de colunas fixas, cadeias A/C, sem ATP/MG, 1 modelo).

    Parametros
    ----------
    cif_path : modelo do Boltz (``..._model_0.cif``) - o MESMO que motivou o par.
    out_dir : diretorio de saida (criado se nao existir).
    name : nome-base do arquivo (default ``reference`` -> ``reference.pdb``).
    esperado : bloco ``chain_roles`` do YAML da campanha (recomendado - ver
        `split_complex`).
    receptor_chains, ligand_chain : papeis EXPLICITOS (ids no `.cif`).
    overwrite : quando False, recusa sobrescrever um arquivo existente.

    Retorno
    -------
    Caminho absoluto (str) do PDB gravado - ja CONFERIDO por
    `check_reference_pdb`. Cofatores (ATP/MG) ficam de fora; a numeracao de
    residuo e a mesma dos PDBs do `split_complex`, porque saem do mesmo modelo.

    Levanta ``ValueError``/``RuntimeError`` nas mesmas condicoes do
    `split_complex` (papeis incoerentes, PDB vazio, cadeia/segid divergente) -
    nunca devolve um arquivo "quase certo".
    """
    saida = Path(out_dir)
    destino = saida / f"{name}.pdb"
    if not overwrite and destino.exists():
        raise FileExistsError(f"arquivo ja existe (overwrite=False): {destino}")

    prep = _preparar_modelo(
        cif_path,
        receptor_chains=receptor_chains,
        ligand_chain=ligand_chain,
        esperado=esperado,
        receptor_chain_out="A",
        ligand_chain_out="C",
    )
    cadeias = [prep["receptor_final"][0], prep["ligante_final"]]
    # keep_hetero=False: o caprieval roda com keep_hetatm=false, e no DEMO01 a
    # cadeia do ATP e justamente 'C' - manter os cofatores so criaria cadeia
    # extra/colisao de id na referencia.
    _write_pdb(prep["model"], cadeias, destino, keep_hetero=False)
    _conferir_cadeias_no_pdb(destino, cadeias, exigir_segid=True)
    check_reference_pdb(destino, cadeias_esperadas=cadeias)
    return str(destino.resolve())


def split_complex(
    cif_path: "str | Path",
    out_dir: "str | Path",
    receptor_name: str = "receptor",
    ligand_name: str = "ligand",
    *,
    keep_hetero: bool = False,
    overwrite: bool = True,
    receptor_chains: "Sequence[str] | None" = None,
    ligand_chain: "str | None" = None,
    esperado: "Mapping[str, Any] | None" = None,
    receptor_chain_out: "str | None" = "A",
    ligand_chain_out: "str | None" = "C",
) -> "Dict[str, Any]":
    """Separa o complexo `.cif` em receptor.pdb + ligand.pdb para o HADDOCK3.

    Papeis, em ordem de precedencia: ``receptor_chains``/``ligand_chain``
    explicitos > ``esperado`` (bloco ``chain_roles`` do YAML da campanha) >
    convencao POSICIONAL do motor (receptor = cadeias proteicas iniciais,
    ligante = a ULTIMA), que e so o fallback. Cadeias sem residuo padrao
    (ATP/MG/agua) nao entram na eleicao - ver a nota sobre o DEMO01 na docstring
    de `funnel.interface`, onde a ordem das cadeias esta invertida em relacao ao
    nome do run e a posicao elegeria a BARSTAR como receptor.

    Parametros
    ----------
    cif_path : modelo do Boltz (``..._model_0.cif``).
    out_dir : diretorio de saida (criado se nao existir).
    receptor_name, ligand_name : nomes-base dos arquivos (sem extensao).
    keep_hetero : ver a secao "Cofatores" na docstring do modulo. Default False.
    overwrite : quando False, recusa sobrescrever PDB ja existente.
    receptor_chains, ligand_chain : papeis EXPLICITOS (ids de cadeia do `.cif`).
        Quando omitidos, valem os de ``esperado`` e, na falta dele, a eleicao
        posicional de `funnel.interface.chain_roles` - que no DEMO01 elege a
        BARSTAR como receptor, ao contrario do que a campanha declara.
    esperado : bloco ``chain_roles`` do YAML da campanha (ver
        `funnel.interface.declared_roles`). Alimenta os papeis E dispara a
        CONFERENCIA contra o modelo (cadeias existem? tamanho bate?), honrando
        ``fail_if_ambiguous``. E o caminho recomendado: os papeis vem da
        campanha declarada, nunca da ordem das cadeias no arquivo.
    receptor_chain_out, ligand_chain_out : cadeia/segid GRAVADOS no PDB
        (default ``"A"``/``"C"``, os segids que o `ambig.tbl` do motor
        referencia). ``None`` preserva o id original - use so para inspecao,
        nunca para alimentar o HADDOCK3.

    Retorno
    -------
    dict com::

        {
          "receptor_pdb": "...\\receptor.pdb",   # caminho absoluto (str)
          "ligand_pdb":   "...\\ligand.pdb",
          "receptor_chains": ["A"],    # id GRAVADO no PDB (= segid)
          "ligand_chain": "C",
          "receptor_chains_origem": ["A"],   # id no .cif de origem
          "ligand_chain_origem": "B",
          "receptor_atoms": 5931,      # conferido REABRINDO o arquivo
          "ligand_atoms": 2874,
          "keep_hetero": False,
          "hetero_residues_kept": 0,   # HETATM mantidos dentro das cadeias
          "dropped_hetero_chains": ["C", "D"],   # cadeias so-hetero descartadas
          "papeis_fonte": "declarado",  # explicito | declarado | posicional
          "receptor_molecule": "barnase",   # so quando declarado no YAML
          "ligand_molecule": "barstar",
        }

    Levanta ``ValueError`` se o `.cif` nao tiver >= 2 cadeias proteicas, se um
    papel explicito apontar cadeia inexistente, se receptor e ligante se
    sobrepuserem, se houver mais de uma cadeia de receptor com renomeacao
    pedida; e ``RuntimeError`` se algum PDB escrito reabrir com zero atomos ou
    com cadeia/segid diferente do contratado.

    Ver tambem `reference_pdb`, que gera do MESMO `.cif` e com os MESMOS papeis o
    PDB do complexo usado como ``reference_fname`` do caprieval.
    """
    saida = Path(out_dir)
    rec_pdb = saida / f"{receptor_name}.pdb"
    lig_pdb = saida / f"{ligand_name}.pdb"
    if rec_pdb.resolve() == lig_pdb.resolve():
        raise ValueError(
            "receptor_name e ligand_name resultam no MESMO arquivo "
            f"({rec_pdb}); um sobrescreveria o outro."
        )
    if not overwrite:
        for p in (rec_pdb, lig_pdb):
            if p.exists():
                raise FileExistsError(f"arquivo ja existe (overwrite=False): {p}")

    prep = _preparar_modelo(
        cif_path,
        receptor_chains=receptor_chains,
        ligand_chain=ligand_chain,
        esperado=esperado,
        receptor_chain_out=receptor_chain_out,
        ligand_chain_out=ligand_chain_out,
    )
    model = prep["model"]
    papeis = prep["papeis"]
    origem_receptor = prep["origem_receptor"]
    origem_ligante = prep["origem_ligante"]
    receptor_final = prep["receptor_final"]
    ligante_final = prep["ligante_final"]

    n_rec = _write_pdb(model, receptor_final, rec_pdb, keep_hetero)
    n_lig = _write_pdb(model, [ligante_final], lig_pdb, keep_hetero)
    _conferir_cadeias_no_pdb(
        rec_pdb, receptor_final, exigir_segid=receptor_chain_out is not None
    )
    _conferir_cadeias_no_pdb(
        lig_pdb, [ligante_final], exigir_segid=ligand_chain_out is not None
    )

    selecionadas = set(receptor_final) | {ligante_final}
    heteros_mantidos = (
        _count_hetero(model, selecionadas) if keep_hetero else 0
    )

    return {
        "receptor_pdb": str(rec_pdb.resolve()),
        "ligand_pdb": str(lig_pdb.resolve()),
        "receptor_chains": receptor_final,
        "ligand_chain": ligante_final,
        "receptor_chains_origem": list(origem_receptor),
        "ligand_chain_origem": origem_ligante,
        "receptor_atoms": n_rec,
        "ligand_atoms": n_lig,
        "keep_hetero": bool(keep_hetero),
        "hetero_residues_kept": heteros_mantidos,
        "dropped_hetero_chains": list(papeis["hetero_chains"]),
        # de onde vieram os papeis: "explicito" (argumentos), "declarado"
        # (bloco chain_roles do YAML) ou "posicional" (fallback do motor).
        "papeis_fonte": (
            "explicito"
            if (receptor_chains or ligand_chain)
            else str(papeis.get("fonte", "posicional"))
        ),
        "receptor_molecule": papeis.get("bait_molecule"),
        "ligand_molecule": papeis.get("partner_molecule"),
    }
