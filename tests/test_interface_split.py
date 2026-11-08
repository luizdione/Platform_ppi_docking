# -*- coding: utf-8 -*-
"""Interface + split: do complexo .cif para os dois PDBs do HADDOCK3.

Estes testes usam um `.cif` SINTETICO (fixture `cif_sintetico`, duas cadeias
proteicas em contato) - sem GPU, sem rede, sem tocar no disco de dados.

O teste-regressao central e `test_pdbparser_le_cif_com_zero_atomos`: ele
DOCUMENTA e trava a armadilha do projeto - `Bio.PDB.PDBParser` aceita um `.cif`
e devolve ZERO atomos em silencio. Se algum dia alguem trocar o `MMCIFParser`
por `PDBParser` dentro do funil, os testes de interface/split ficam vazios e
falham aqui.
"""

from __future__ import annotations

import warnings

import pytest

from funnel import interface, split


# --------------------------------------------------------------------------
# regressao: o parser errado nao pode ser usado
# --------------------------------------------------------------------------
def test_pdbparser_le_cif_com_zero_atomos(cif_sintetico) -> None:
    """A armadilha existe de verdade: PDBParser no .cif = 0 atomos, sem erro."""
    from Bio.PDB.MMCIFParser import MMCIFParser
    from Bio.PDB.PDBParser import PDBParser

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        pela_via_errada = PDBParser(QUIET=True).get_structure("x", str(cif_sintetico))
        n_errado = sum(1 for _ in pela_via_errada.get_atoms())

    pela_via_certa = MMCIFParser(QUIET=True).get_structure("x", str(cif_sintetico))
    n_certo = sum(1 for _ in pela_via_certa.get_atoms())

    assert n_errado == 0, "o PDBParser passou a ler .cif - reveja a premissa do modulo"
    assert n_certo > 0


def test_funil_usa_o_parser_certo(cif_sintetico) -> None:
    """O funil le o MESMO .cif com atomos - prova que nao caiu na armadilha."""
    from scoring_engine import _common

    modelo = _common.parse_model(cif_sintetico)
    assert sum(1 for _ in modelo.get_atoms()) > 0


def test_extensao_pdb_e_recusada(tmp_path) -> None:
    """Passar um .pdb onde se espera .cif e erro claro, nao leitura vazia."""
    falso = tmp_path / "modelo.pdb"
    falso.write_text("ATOM\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mmCIF"):
        interface.iface_residues(falso)


def test_cif_inexistente_falha_alto(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        interface.iface_residues(tmp_path / "nao_existe.cif")


# --------------------------------------------------------------------------
# eleicao de papeis e residuos de interface
# --------------------------------------------------------------------------
def test_chain_roles_elege_a_ultima_cadeia_como_parceiro(cif_sintetico) -> None:
    """Convencao posicional do motor: parceiro = ULTIMA cadeia proteica."""
    papeis = interface.chain_roles(cif_sintetico)
    assert papeis["bait_chains"] == ["A"]
    assert papeis["partner_chain"] == "B"
    assert papeis["chain_sizes"] == {"A": 5, "B": 4}
    assert papeis["hetero_chains"] == []


def test_iface_residues_acha_a_cadeia_parceira_como_ultima(cif_sintetico) -> None:
    """iface_residues devolve contatos reais entre a isca e a ULTIMA cadeia."""
    res = interface.iface_residues(cif_sintetico)
    assert res["bait_chains"] == ["A"]
    assert res["partner_chain"] == "B"
    assert res["n_pairs"] > 0
    assert res["bait_residues"], "nenhum residuo ativo da isca"
    assert res["partner_residues"], "nenhum residuo ativo do parceiro"
    # os numeros vem do modelo: cadeia A = 10..14, cadeia B = 30..33
    assert all(10 <= r <= 14 for r in res["bait_residues"])
    assert all(30 <= r <= 33 for r in res["partner_residues"])
    assert res["cutoff"] == 5.0
    assert res["protein_only"] is True


def test_interface_vazia_falha_alto_por_padrao(tmp_path) -> None:
    """Sem contato entre as cadeias, AIR vazio faria o HADDOCK rodar solto."""
    from Bio.PDB.Chain import Chain
    from Bio.PDB.mmcifio import MMCIFIO
    from Bio.PDB.Model import Model
    from Bio.PDB.Structure import Structure

    from tests.conftest import _residuo

    est = Structure("longe")
    modelo = Model(0)
    est.add(modelo)
    serial = 1
    for cid, deslocamento in (("A", 0.0), ("B", 500.0)):
        cadeia = Chain(cid)
        for i in range(3):
            res, serial = _residuo("ALA", 10 + i, (deslocamento, 4.0 * i, 0.0), serial)
            cadeia.add(res)
        modelo.add(cadeia)

    destino = tmp_path / "sem_contato.cif"
    io = MMCIFIO()
    io.set_structure(est)
    io.save(str(destino))

    with pytest.raises(ValueError, match="NENHUM contato"):
        interface.iface_residues(destino)

    # com strict=False e inspecao, nao veredicto: listas vazias explicitas
    solto = interface.iface_residues(destino, strict=False)
    assert solto["n_pairs"] == 0
    assert solto["bait_residues"] == []


def test_monomero_nao_tem_interface(tmp_path) -> None:
    """Uma unica cadeia proteica nao produz par isca x parceiro."""
    from Bio.PDB.Chain import Chain
    from Bio.PDB.mmcifio import MMCIFIO
    from Bio.PDB.Model import Model
    from Bio.PDB.Structure import Structure

    from tests.conftest import _residuo

    est = Structure("mono")
    modelo = Model(0)
    est.add(modelo)
    cadeia = Chain("A")
    serial = 1
    for i in range(3):
        res, serial = _residuo("ALA", 10 + i, (0.0, 4.0 * i, 0.0), serial)
        cadeia.add(res)
    modelo.add(cadeia)
    destino = tmp_path / "mono.cif"
    io = MMCIFIO()
    io.set_structure(est)
    io.save(str(destino))

    with pytest.raises(ValueError, match="cadeia"):
        interface.chain_roles(destino)


# --------------------------------------------------------------------------
# split: dois PDBs NAO vazios
# --------------------------------------------------------------------------
def test_split_gera_dois_pdbs_com_atomos(cif_sintetico, tmp_path) -> None:
    """split_complex escreve receptor.pdb + ligand.pdb, ambos com atomos.

    Os ids GRAVADOS sao os segids que o `ambig.tbl` do motor referencia
    (receptor=A, parceiro=C); os ids de ORIGEM continuam reportados (A e B).
    """
    from pathlib import Path

    saida = split.split_complex(cif_sintetico, tmp_path)

    assert saida["receptor_chains"] == ["A"]
    assert saida["ligand_chain"] == "C"
    assert saida["receptor_chains_origem"] == ["A"]
    assert saida["ligand_chain_origem"] == "B"
    assert saida["receptor_atoms"] > 0
    assert saida["ligand_atoms"] > 0

    rec, lig = Path(saida["receptor_pdb"]), Path(saida["ligand_pdb"])
    assert rec.is_file() and lig.is_file()
    assert rec.stat().st_size > 0 and lig.stat().st_size > 0

    # confere REABRINDO (agora sao PDBs de verdade -> PDBParser e o parser certo)
    from Bio.PDB.PDBParser import PDBParser

    p = PDBParser(QUIET=True)
    n_rec = sum(1 for _ in p.get_structure("r", str(rec)).get_atoms())
    n_lig = sum(1 for _ in p.get_structure("l", str(lig)).get_atoms())
    assert n_rec == saida["receptor_atoms"] > 0
    assert n_lig == saida["ligand_atoms"] > 0
    # cada PDB tem SO a sua cadeia
    cadeias_rec = {c.id for c in p.get_structure("r", str(rec)).get_chains()}
    cadeias_lig = {c.id for c in p.get_structure("l", str(lig)).get_chains()}
    assert cadeias_rec == {"A"}
    assert cadeias_lig == {"C"}


def test_split_grava_os_segids_do_ambig_tbl(cif_sintetico, tmp_path) -> None:
    """Colunas 73-76 do PDB tem de trazer 'A' e 'C' - o ambig.tbl depende disso."""

    def _segids(caminho: str) -> set:
        return {
            linha[72:76].strip()
            for linha in open(caminho, encoding="utf-8").read().splitlines()
            if linha.startswith(("ATOM  ", "HETATM"))
        }

    saida = split.split_complex(cif_sintetico, tmp_path)
    assert _segids(saida["receptor_pdb"]) == {"A"}
    assert _segids(saida["ligand_pdb"]) == {"C"}


def test_split_sem_renomear_preserva_os_ids_de_origem(cif_sintetico, tmp_path) -> None:
    """Modo inspecao (*_chain_out=None): cadeias saem como estao no .cif."""
    saida = split.split_complex(
        cif_sintetico, tmp_path, receptor_chain_out=None, ligand_chain_out=None
    )
    assert saida["receptor_chains"] == ["A"]
    assert saida["ligand_chain"] == "B"


def test_split_numeracao_bate_com_a_interface(cif_sintetico, tmp_path) -> None:
    """Os residuos ativos existem, com o MESMO numero, dentro dos PDBs gerados."""
    from Bio.PDB.PDBParser import PDBParser

    res = interface.iface_residues(cif_sintetico)
    saida = split.split_complex(cif_sintetico, tmp_path)

    p = PDBParser(QUIET=True)
    numeros_rec = {
        r.id[1] for r in p.get_structure("r", saida["receptor_pdb"]).get_residues()
    }
    numeros_lig = {
        r.id[1] for r in p.get_structure("l", saida["ligand_pdb"]).get_residues()
    }
    assert set(res["bait_residues"]) <= numeros_rec
    assert set(res["partner_residues"]) <= numeros_lig


def test_split_recusa_sobrescrever_quando_pedido(cif_sintetico, tmp_path) -> None:
    split.split_complex(cif_sintetico, tmp_path)
    with pytest.raises(FileExistsError):
        split.split_complex(cif_sintetico, tmp_path, overwrite=False)


def test_split_recusa_nomes_iguais(cif_sintetico, tmp_path) -> None:
    """Mesmo nome para receptor e ligante sobrescreveria um deles."""
    with pytest.raises(ValueError):
        split.split_complex(cif_sintetico, tmp_path, receptor_name="x", ligand_name="x")


# --------------------------------------------------------------------------
# papeis DECLARADOS pela campanha vencem a ordem das cadeias
# --------------------------------------------------------------------------
# No `.cif` sintetico a cadeia A tem 5 residuos (10..14) e a B tem 4 (30..33).
# A convencao posicional elege A como isca; a campanha abaixo declara o
# INVERSO - e e ela que tem de valer (e o caso do DEMO01: A = barstar 760 res,
# B = barnase 376 res, com a barnase declarada como receptor).
_DECLARADO_INVERTIDO = {
    "receptor": {"molecule": "receptor_de_mentira", "chain": "B", "n_residues_esperado": 4},
    "ligand": {"molecule": "miosina_de_mentira", "chain": "A", "n_residues_esperado_min": 5},
    "fail_if_ambiguous": True,
}


def test_papeis_declarados_vencem_a_posicao(cif_sintetico) -> None:
    """Com `esperado`, receptor/ligante saem do YAML - nao da ordem no arquivo."""
    posicional = interface.chain_roles(cif_sintetico)
    assert posicional["bait_chains"] == ["A"] and posicional["fonte"] == "posicional"

    declarado = interface.chain_roles(cif_sintetico, esperado=_DECLARADO_INVERTIDO)
    assert declarado["bait_chains"] == ["B"]
    assert declarado["partner_chain"] == "A"
    assert declarado["fonte"] == "declarado"
    assert declarado["bait_molecule"] == "receptor_de_mentira"


def test_iface_residues_honra_os_papeis_explicitos(cif_sintetico) -> None:
    """Papel explicito manda: a isca passa a ser a cadeia B (residuos 30..33)."""
    res = interface.iface_residues(cif_sintetico, bait_chains=["B"], partner_chain="A")
    assert res["bait_chains"] == ["B"]
    assert res["partner_chain"] == "A"
    assert res["fonte"] == "explicito"
    assert res["n_pairs"] > 0
    assert all(30 <= r <= 33 for r in res["bait_residues"])
    assert all(10 <= r <= 14 for r in res["partner_residues"])


def test_iface_residues_recusa_cadeia_inexistente(cif_sintetico) -> None:
    """Cadeia pedida que nao existe (ou nao e proteica) = erro claro."""
    with pytest.raises(ValueError, match="Z"):
        interface.iface_residues(cif_sintetico, bait_chains=["Z"], partner_chain="A")
    with pytest.raises(ValueError, match="isca E como parceiro"):
        interface.iface_residues(cif_sintetico, bait_chains=["A"], partner_chain="A")


def test_declaracao_com_cadeia_ausente_falha_alto(cif_sintetico) -> None:
    """Cadeia declarada inexistente nunca cai de volta na eleicao posicional."""
    esperado = {
        "receptor": {"chain": "Z", "n_residues_esperado": 4},
        "ligand": {"chain": "A"},
    }
    with pytest.raises(ValueError, match="nao"):
        interface.chain_roles(cif_sintetico, esperado=esperado)


def test_tamanho_divergente_falha_alto_com_fail_if_ambiguous(cif_sintetico) -> None:
    """n_residues_esperado fora da tolerancia = erro (nunca chute pela ordem)."""
    esperado = {
        "receptor": {"chain": "B", "n_residues_esperado": 376},
        "ligand": {"chain": "A", "n_residues_esperado_min": 600},
        "fail_if_ambiguous": True,
    }
    with pytest.raises(ValueError, match="NAO batem"):
        interface.chain_roles(cif_sintetico, esperado=esperado)

    # com fail_if_ambiguous=false a divergencia vira aviso EXPLICITO, nunca silencio
    frouxo = dict(esperado, fail_if_ambiguous=False)
    with pytest.warns(RuntimeWarning, match="NAO batem"):
        papeis = interface.chain_roles(cif_sintetico, esperado=frouxo)
    assert papeis["bait_chains"] == ["B"]


def test_split_usa_os_papeis_declarados(cif_sintetico, tmp_path) -> None:
    """O receptor.pdb passa a conter a cadeia DECLARADA (B), gravada como segid A."""
    from Bio.PDB.PDBParser import PDBParser

    saida = split.split_complex(cif_sintetico, tmp_path, esperado=_DECLARADO_INVERTIDO)
    assert saida["receptor_chains_origem"] == ["B"]
    assert saida["ligand_chain_origem"] == "A"
    assert saida["papeis_fonte"] == "declarado"
    # o contrato com o ambig.tbl continua intacto: A (receptor) e C (ligante)
    assert saida["receptor_chains"] == ["A"] and saida["ligand_chain"] == "C"

    p = PDBParser(QUIET=True)
    numeros = {r.id[1] for r in p.get_structure("r", saida["receptor_pdb"]).get_residues()}
    assert numeros == {30, 31, 32, 33}, "o receptor.pdb nao e a cadeia declarada"


# --------------------------------------------------------------------------
# referencia do caprieval: PDB do COMPLEXO (A+C), nunca o .cif
# --------------------------------------------------------------------------
def test_reference_pdb_e_um_pdb_valido_com_as_duas_cadeias(cif_sintetico, tmp_path) -> None:
    """A referencia sai como PDB de colunas fixas, com cadeias A e C e atomos."""
    from pathlib import Path

    from Bio.PDB.PDBParser import PDBParser

    caminho = Path(split.reference_pdb(cif_sintetico, tmp_path))
    assert caminho.is_file() and caminho.suffix == ".pdb"
    assert caminho.stat().st_size > 0

    p = PDBParser(QUIET=True)
    estrutura = p.get_structure("ref", str(caminho))
    cadeias = {c.id for c in estrutura.get_chains()}
    n_atoms = sum(1 for _ in estrutura.get_atoms())
    assert cadeias == {"A", "C"}, "a referencia tem de ter os IDs dos modelos do HADDOCK"
    assert n_atoms > 0

    # e legivel do jeito que o haddock3 le: int(line[22:26]) em TODA linha ATOM
    info = split.check_reference_pdb(caminho)
    assert info["n_atoms"] == n_atoms
    assert sorted(info["chains"]) == ["A", "C"]
    assert info["n_models"] <= 1


def test_reference_pdb_e_o_complexo_e_nao_so_o_receptor(cif_sintetico, tmp_path) -> None:
    """As DUAS moleculas no MESMO arquivo - senao nao ha interface para comparar.

    Alem de conter as duas cadeias, os numeros de residuo tem de ser os mesmos
    dos PDBs de entrada: e assim que a cadeia 'A' da referencia continua sendo a
    mesma molecula do `receptor.pdb` (o caprieval resolve `receptor_chain` sobre
    a nomenclatura da REFERENCIA).
    """
    referencia = split.reference_pdb(cif_sintetico, tmp_path / "ref")
    pdbs = split.split_complex(cif_sintetico, tmp_path / "pdbs")

    por_cadeia = split.residues_by_chain(referencia)
    assert set(por_cadeia) == {"A", "C"}
    assert por_cadeia["A"] and por_cadeia["C"]

    do_receptor = split.residues_by_chain(pdbs["receptor_pdb"])["A"]
    do_ligante = split.residues_by_chain(pdbs["ligand_pdb"])["C"]
    assert por_cadeia["A"] == do_receptor
    assert por_cadeia["C"] == do_ligante
    # e as duas moleculas sao DIFERENTES (nao e o receptor duplicado)
    assert por_cadeia["A"] != por_cadeia["C"]


def test_reference_pdb_honra_os_papeis_declarados(cif_sintetico, tmp_path) -> None:
    """Com a campanha declarada, a cadeia A da referencia e o receptor declarado."""
    referencia = split.reference_pdb(
        cif_sintetico, tmp_path, esperado=_DECLARADO_INVERTIDO
    )
    por_cadeia = split.residues_by_chain(referencia)
    assert por_cadeia["A"] == [30, 31, 32, 33], "a cadeia A nao e o receptor declarado"
    assert por_cadeia["C"] == [10, 11, 12, 13, 14]


def test_check_reference_pdb_recusa_o_cif(cif_sintetico) -> None:
    """O guard que faltava no haddock3: mmCIF como referencia = erro AQUI.

    `libpdb.handle_input_reference` do haddock3 aceita um mmCIF (as linhas do
    loop `_atom_site` comecam com ATOM/HETATM) e so quebra la dentro. Aqui a
    recusa e imediata e explicita.
    """
    with pytest.raises(ValueError, match="colunas fixas"):
        split.check_reference_pdb(cif_sintetico)


def test_residues_by_chain_recusa_conteudo_de_cif(cif_sintetico, tmp_path) -> None:
    """Conteudo mmCIF renomeado para .pdb tambem tem de falhar (nao e a extensao)."""
    disfarcado = tmp_path / "disfarcado.pdb"
    disfarcado.write_text(
        cif_sintetico.read_text(encoding="utf-8"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=r"\[22:26\]"):
        split.residues_by_chain(disfarcado)


def test_reference_pdb_recusa_sobrescrever_quando_pedido(cif_sintetico, tmp_path) -> None:
    split.reference_pdb(cif_sintetico, tmp_path)
    with pytest.raises(FileExistsError):
        split.reference_pdb(cif_sintetico, tmp_path, overwrite=False)
