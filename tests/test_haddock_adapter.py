# -*- coding: utf-8 -*-
"""Adaptador HADDOCK3: `prepare()` e logica pura e roda SEM o binario haddock3.

O que estes testes travam:

* `.cfg` e `ambig.tbl` sao gerados, nao-vazios, a partir de PDBs sinteticos;
* o `.cfg` traz `reference_fname` - sem ele o DockQ do capri_clt sai
  auto-referenciado e o GATE 2 decidiria sobre um numero vazio de significado;
* e essa referencia e um **PDB** do complexo (cadeias A+C), NUNCA o `.cif`: o
  haddock3 le a referencia fatiando colunas de PDB e um mmCIF derruba o modulo
  caprieval inteiro (`invalid literal for int() with base 10: ' A -'`);
* o `.tbl` tem AIRs BIDIRECIONAIS (assign no segid A e assign no segid C), que e
  o formato ativo-ativo proteina-proteina;
* manifesto sem residuo ativo / sem referencia falha ALTO, nao gera arquivo
  plausivel;
* com o binario ausente, `run()` devolve status "error" - nunca sucesso - mas
  preserva os artefatos ja preparados.
"""

from __future__ import annotations

import pathlib
from pathlib import Path

import pytest

from funnel import split
from funnel.contract import PairManifest
from funnel.haddock_adapter import (
    build_request,
    check_toolchain,
    docking_suite_home,
    expected_run_dir,
    prepare,
    run,
    run_dir_name,
)

pytestmark = pytest.mark.skipif(
    not (docking_suite_home() / "engines" / "__init__.py").is_file(),
    reason=f"repo dos engines ausente em {docking_suite_home()}",
)


@pytest.fixture()
def manifesto(cif_sintetico, tmp_path) -> PairManifest:
    """Manifesto pronto para docking: PDBs em disco + residuos ativos reais."""
    from funnel import interface

    pdbs = split.split_complex(cif_sintetico, tmp_path / "pdbs")
    res = interface.iface_residues(cif_sintetico)
    return PairManifest(
        campaign="DEMO01_barnase_barstar",
        pair_id="barnase_x_barstar",
        bait="barnase",
        partner="barstar",
        cif_model=str(cif_sintetico),
        receptor_pdb=pdbs["receptor_pdb"],
        ligand_pdb=pdbs["ligand_pdb"],
        active_residues=res["bait_residues"],
        partner_active_residues=res["partner_residues"],
        stage="gated",
    )


# --------------------------------------------------------------------------
# prepare - logica pura
# --------------------------------------------------------------------------
def test_prepare_gera_cfg_e_tbl_nao_vazios(manifesto, tmp_path) -> None:
    """Sem o binario haddock3, os dois artefatos saem prontos em disco."""
    art = prepare(manifesto, tmp_path / "work")
    cfg, tbl = Path(art.cfg), Path(art.ambig_tbl)
    assert cfg.is_file() and tbl.is_file()
    assert cfg.stat().st_size > 0
    assert tbl.stat().st_size > 0
    assert art.run_dir is None  # so nasce quando o haddock3 roda


def test_cfg_tem_caprieval_reference(manifesto, tmp_path) -> None:
    """A referencia do caprieval existe e e um PDB - sem ela o DockQ e inutil."""
    art = prepare(manifesto, tmp_path / "work")
    texto = Path(art.cfg).read_text(encoding="utf-8")
    assert "reference_fname" in texto
    linha = texto.split("reference_fname")[1].splitlines()[0]
    # o caminho e normalizado para barra normal (o .cfg interpola cru)
    assert "\\" not in linha
    assert Path(manifesto.reference_pdb).as_posix() in texto


def test_cfg_nunca_referencia_o_cif(manifesto, tmp_path) -> None:
    """REGRESSAO: apontar o reference_fname para o .cif derruba o caprieval.

    O haddock3 le a referencia como PDB de colunas fixas (`libpdb.slc_resseq =
    slice(22,26)`); num mmCIF do Boltz essa fatia vale ``' A -'`` e cada worker
    do caprieval morre com ``invalid literal for int()``, ate o modulo estourar
    com ``'NoneType' object has no attribute 'identificator'``. Se alguem voltar
    a passar o `.cif`, este teste quebra.
    """
    art = prepare(manifesto, tmp_path / "work")
    texto = Path(art.cfg).read_text(encoding="utf-8")
    linha = texto.split("reference_fname")[1].splitlines()[0]
    assert ".cif" not in linha, "reference_fname voltou a apontar para um mmCIF"
    assert linha.strip().endswith('.pdb"')
    assert Path(manifesto.cif_model).as_posix() not in texto
    assert Path(manifesto.reference_pdb).is_file()


def test_cfg_declara_os_dois_pdbs_e_o_ambig(manifesto, tmp_path) -> None:
    """molecules = [receptor, ligand] e ambig_fname nos modulos de refinamento."""
    art = prepare(manifesto, tmp_path / "work")
    texto = Path(art.cfg).read_text(encoding="utf-8")
    assert Path(manifesto.receptor_pdb).as_posix() in texto
    assert Path(manifesto.ligand_pdb).as_posix() in texto
    assert 'ambig_fname = "ambig.tbl"' in texto
    assert "[rigidbody]" in texto
    # protein_protein=True: sem topologia CNS de ligante pequeno
    assert "ligand_param_fname" not in texto
    assert "ligand_top_fname" not in texto


def test_tbl_tem_airs_bidirecionais(manifesto, tmp_path) -> None:
    """AIR ativo-ativo: assign a partir do segid A E a partir do segid C."""
    art = prepare(manifesto, tmp_path / "work")
    texto = Path(art.ambig_tbl).read_text(encoding="utf-8")
    linhas_assign = [l for l in texto.splitlines() if l.startswith("assign")]
    assert linhas_assign, "ambig.tbl sem nenhuma restricao"
    do_receptor = [l for l in linhas_assign if "segid A" in l]
    do_parceiro = [l for l in linhas_assign if "segid C" in l]
    assert do_receptor, "faltam AIRs partindo do receptor (segid A)"
    assert do_parceiro, "faltam AIRs partindo do parceiro (segid C)"
    assert len(do_receptor) == len(manifesto.active_residues)
    assert len(do_parceiro) == len(manifesto.partner_active_residues)
    # cada residuo ativo aparece no arquivo com o numero real
    for r in manifesto.active_residues:
        assert f"resid {r} and segid A" in texto
    for r in manifesto.partner_active_residues:
        assert f"resid {r} and segid C" in texto


def test_run_dir_previsto_e_deterministico(manifesto, tmp_path) -> None:
    """O nome do run_dir sai de bait/partner, sem depender do binario."""
    assert run_dir_name(manifesto) == "run-barnase-barstar"
    assert expected_run_dir(manifesto, tmp_path).name == run_dir_name(manifesto)


def test_build_request_mapeia_o_manifesto(manifesto, tmp_path) -> None:
    """EngineRequest recebe exatamente o que o motor espera (PPI + referencia)."""
    req = build_request(manifesto, tmp_path)
    assert req.kind == "docking"
    assert req.params["protein_protein"] is True
    assert req.params["active_residues"] == manifesto.active_residues
    assert req.params["partner_active_residues"] == manifesto.partner_active_residues
    assert req.params["caprieval_reference"].endswith("reference.pdb")
    assert not req.params["caprieval_reference"].endswith(".cif")
    assert req.inputs["receptor_pdb"].endswith("receptor.pdb")
    assert req.inputs["ligand_pdb"].endswith("ligand.pdb")


def test_build_request_gera_a_referencia_quando_falta(manifesto, tmp_path) -> None:
    """Manifesto sem `reference_pdb`: o PDB do complexo nasce aqui, conferido."""
    from funnel import split as _split

    assert manifesto.reference_pdb is None
    req = build_request(manifesto, tmp_path)

    gerada = Path(req.params["caprieval_reference"])
    assert gerada.is_file()
    assert manifesto.reference_pdb == str(gerada.resolve()) or Path(
        manifesto.reference_pdb
    ).resolve() == gerada.resolve()
    info = _split.check_reference_pdb(gerada)
    assert sorted(info["chains"]) == ["A", "C"]
    assert info["n_atoms"] > 0


def test_referencia_com_papeis_trocados_falha_alto(manifesto, tmp_path) -> None:
    """Referencia gerada com receptor/ligante invertidos = DockQ trocado.

    O `receptor_chain` do caprieval e resolvido sobre a nomenclatura da
    REFERENCIA: uma referencia invertida daria L-RMSD/DockQ plausiveis e errados.
    Aqui isso tem de virar erro, nao numero.
    """
    from funnel import split as _split

    invertida = _split.reference_pdb(
        manifesto.cif_model,
        tmp_path / "ruim",
        receptor_chains=["B"],   # no .cif sintetico, A e a isca posicional
        ligand_chain="A",
    )
    manifesto.reference_pdb = invertida
    with pytest.raises(ValueError, match="referencia do caprieval"):
        build_request(manifesto, tmp_path)


def test_referencia_apontando_para_cif_falha_alto(manifesto, tmp_path) -> None:
    """Se alguem gravar o .cif no campo da referencia, quebra AQUI e nao no CNS."""
    manifesto.reference_pdb = str(manifesto.cif_model)
    with pytest.raises(ValueError, match="colunas fixas"):
        build_request(manifesto, tmp_path)


# --------------------------------------------------------------------------
# recusas explicitas (nunca artefato plausivel)
# --------------------------------------------------------------------------
def test_sem_residuo_ativo_falha_alto(manifesto, tmp_path) -> None:
    manifesto.active_residues = []
    with pytest.raises(ValueError, match="active_residues"):
        prepare(manifesto, tmp_path / "work")


def test_sem_residuo_do_parceiro_falha_alto(manifesto, tmp_path) -> None:
    manifesto.partner_active_residues = []
    with pytest.raises(ValueError, match="partner_active_residues"):
        prepare(manifesto, tmp_path / "work")


def test_sem_cif_de_referencia_falha_alto(manifesto, tmp_path) -> None:
    """Sem referencia o caprieval fica auto-referenciado - melhor nem preparar."""
    manifesto.cif_model = None
    with pytest.raises(ValueError, match="cif_model"):
        prepare(manifesto, tmp_path / "work")


def test_pdb_inexistente_falha_alto(manifesto, tmp_path) -> None:
    manifesto.receptor_pdb = str(tmp_path / "nao_existe.pdb")
    with pytest.raises(FileNotFoundError):
        prepare(manifesto, tmp_path / "work")


# --------------------------------------------------------------------------
# run - depende do binario; pulado onde ele nao existe
# --------------------------------------------------------------------------
def test_dry_run_nao_executa_nada(manifesto, tmp_path) -> None:
    """dry_run devolve so o cmdline, mas os artefatos ja ficam prontos.

    `motor="local"` EXPLICITO: o caminho Docker exige o workdir sob a raiz de
    dados montada (a env ``PPI_DOCK_DATA_ROOT``), e `tmp_path` nao esta -- ver
    `test_motor_docker_recusa_workdir_fora_da_raiz`.
    """
    resultado = run(manifesto, tmp_path / "work", dry_run=True, motor="local")
    assert resultado.status == "ok"
    assert resultado.cmdline[0] == "haddock3"
    assert resultado.outputs["prepared"] is True
    assert Path(resultado.outputs["cfg"]).is_file()


def test_run_sem_binario_falha_alto_e_preserva_artefatos(manifesto, tmp_path) -> None:
    """Binario ausente = status 'error' com cfg/tbl guardados - nunca sucesso."""
    if check_toolchain().get("available"):
        pytest.skip("haddock3 esta instalado nesta maquina; este teste cobre a ausencia")
    resultado = run(manifesto, tmp_path / "work", motor="local")
    assert resultado.status == "error"
    assert resultado.error
    assert "haddock3" in resultado.error
    assert Path(resultado.outputs["cfg"]).is_file()
    assert Path(resultado.outputs["ambig_tbl"]).is_file()


def test_motor_invalido_erra_alto(manifesto, tmp_path) -> None:
    """Motor desconhecido e erro de programacao: ValueError, nao fallback mudo."""
    with pytest.raises(ValueError, match="motor invalido"):
        run(manifesto, tmp_path / "work", motor="haddock2")


def _raiz_de_dados_separada(monkeypatch, tmp_path) -> pathlib.Path:
    """Declara uma raiz de dados que NAO contem o workdir do teste.

    `PPI_DOCK_DATA_ROOT` nao tem default (um caminho fixo apontaria para o disco
    de quem escreveu o codigo). Estes testes precisam dela declarada, e precisam
    que o workdir caia FORA dela -- que e exatamente a situacao sob teste.
    """
    raiz = tmp_path / "raiz_de_dados"
    raiz.mkdir()
    monkeypatch.setenv("PPI_DOCK_DATA_ROOT", str(raiz))
    return raiz


def test_motor_docker_recusa_workdir_fora_da_raiz(
    manifesto, tmp_path, monkeypatch
) -> None:
    """Workdir fora da raiz de dados nao e visivel no container -> erro claro.

    O container monta UMA raiz (a do dado do projeto). Um `.cfg` apontando para
    fora dela faria o HADDOCK3 nao achar as moleculas; melhor recusar aqui, com
    mensagem util, do que descobrir no meio da execucao.
    """
    _raiz_de_dados_separada(monkeypatch, tmp_path)
    resultado = run(manifesto, tmp_path / "work", motor="docker", dry_run=True)
    assert resultado.status == "error"
    assert "fora da raiz montada" in (resultado.error or "")
    # o prepare rodou antes: os artefatos continuam preservados
    assert Path(resultado.outputs["cfg"]).is_file()


def test_motor_auto_escolhe_docker_quando_nao_ha_binario(
    manifesto, tmp_path, monkeypatch
) -> None:
    """Sem binario local mas com imagem, 'auto' vai para o container.

    O workdir tmp esta fora da raiz montada, entao o resultado e erro -- mas a
    MENSAGEM prova qual motor foi escolhido (a do docker, nao a do binario).
    """
    from funnel import haddock_docker as _hd

    if check_toolchain().get("available"):
        pytest.skip("ha binario local; 'auto' escolheria o local")
    if not _hd.check_image().get("available"):
        pytest.skip("imagem ppi-haddock3 ausente; 'auto' nao tem para onde ir")
    _raiz_de_dados_separada(monkeypatch, tmp_path)
    resultado = run(manifesto, tmp_path / "work", dry_run=True)
    assert resultado.status == "error"
    assert "fora da raiz montada" in (resultado.error or "")


def test_check_toolchain_reporta_estado(tmp_path) -> None:
    """check_toolchain nao interpreta: so repassa o dict do motor."""
    estado = check_toolchain()
    assert isinstance(estado.get("available"), bool)
    assert "haddock3" in estado.get("tools", {})
