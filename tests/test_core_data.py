# -*- coding: utf-8 -*-
"""Nucleo de dados (`core/data.py`): carrega SEM framework de UI e monta a
tabela do funil.

O nucleo alimenta tanto o dashboard (`web/`, FastAPI) quanto o gerador do
viewer estatico, entao nao pode arrastar dependencia de interface. O primeiro
teste roda num subprocesso com o import de `streamlit` BLOQUEADO, o que e prova
de verdade (e nao "streamlit por acaso nao estava carregado ainda").
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from core import data as gui_core
from core.data import (
    GuiError,
    PairRef,
    Subproject,
    TABLE_COLUMNS,
    approved_rows,
    build_funnel_table,
    discover_pairs,
    load_scores_tsv,
    load_subprojects,
    read_text_artifact,
)
from funnel.contract import GateResult, PairManifest, Scores

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# 1. importa sem streamlit
# --------------------------------------------------------------------------
def test_core_data_importa_sem_streamlit() -> None:
    """Com o import de streamlit bloqueado, core.data ainda carrega."""
    codigo = textwrap.dedent(
        """
        import sys

        class _Bloqueia:
            def find_module(self, nome, caminho=None):
                return self.find_spec(nome, caminho)

            def find_spec(self, nome, caminho=None, alvo=None):
                if nome == "streamlit" or nome.startswith("streamlit."):
                    raise ImportError("streamlit bloqueado de proposito no teste")
                return None

        sys.meta_path.insert(0, _Bloqueia())
        import core.data as g
        assert "streamlit" not in sys.modules
        assert callable(g.build_funnel_table)
        print("OK")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout


def test_core_data_nao_importa_streamlit_no_topo() -> None:
    """Guarda de codigo: nenhum 'import streamlit' no modulo testavel."""
    fonte = (REPO_ROOT / "core" / "data.py").read_text(encoding="utf-8")
    assert "import streamlit" not in fonte


# --------------------------------------------------------------------------
# 2. tabela do funil
# --------------------------------------------------------------------------
def _par(pair_id: str, problemas=None) -> PairRef:
    return PairRef(
        pair_id=pair_id,
        run_dir=f"/dados/demo01/resultados/boltz_results_{pair_id}",
        cif_model=f"/dados/demo01/resultados/{pair_id}_model_0.cif",
        problems=list(problemas or []),
    )


def _manifesto(pair_id: str, aprovado: bool) -> PairManifest:
    return PairManifest(
        campaign="DEMO01_barnase_barstar",
        pair_id=pair_id,
        bait="barnase",
        partner="barstar",
        scores=Scores(
            ipsae=0.616,
            iptm=0.8062894940376282,
            coverage_core_face=0.966,
            face="nuclear (canonica)",
            n_iface_res=29,
        ),
        gate_boltz=GateResult(
            approved=aprovado,
            reasons=["ipSAE 0.616 >= 0.15", "VEREDICTO: aprovado para HADDOCK3"],
        ),
        stage="gated",
    )


def test_build_funnel_table_produz_linhas() -> None:
    """Uma linha por par, com todas as colunas declaradas em TABLE_COLUMNS."""
    pares = [_par("barnase_x_barstar"), _par("barnase_x_barstarW44F_ATP")]
    manifestos = {
        "barnase_x_barstar": _manifesto(
            "barnase_x_barstar", True
        )
    }
    scores = {
        "barnase_x_barstarW44F_ATP": {
            "ipsae": 0.011,
            "iptm": 0.5107879638671875,
            "coverage_core_face": 0.943,
            "face": "nuclear (canonica)",
            "n_iface_res": 35,
            "manual": False,
        }
    }
    linhas = build_funnel_table(pares, scores, manifestos)

    assert len(linhas) == 2
    for linha in linhas:
        for coluna in TABLE_COLUMNS:
            assert coluna in linha

    avaliado, nao_avaliado = linhas[0], linhas[1]
    assert avaliado["gate"] == "APROVADO"
    assert avaliado["aprovado"] is True
    assert avaliado["ipSAE"] == 0.616
    assert avaliado["estagio"] == "gated"
    assert "ipSAE 0.616 >= 0.15" in avaliado["motivos"]

    # sem manifesto o gate NAO e recalculado aqui
    assert nao_avaliado["gate"] == "nao avaliado"
    assert nao_avaliado["aprovado"] is None
    assert nao_avaliado["ipSAE"] == 0.011
    assert nao_avaliado["manual"] is False


def test_manifesto_tem_precedencia_sobre_a_planilha() -> None:
    """O que o funil REALMENTE usou vale mais que a avaliacao previa."""
    pid = "barnase_x_barstar"
    linhas = build_funnel_table(
        [_par(pid)],
        {pid: {"ipsae": 0.111, "face": "planilha antiga"}},
        {pid: _manifesto(pid, True)},
    )
    assert linhas[0]["ipSAE"] == 0.616
    assert linhas[0]["face"] == "nuclear (canonica)"


def test_problemas_do_par_aparecem_na_tabela() -> None:
    """Par com arquivo faltando continua listado, marcado - nunca some."""
    linhas = build_funnel_table([_par("par_quebrado", ["PAE ausente: pae_x.npz"])])
    assert len(linhas) == 1
    assert "PAE ausente" in linhas[0]["problemas"]


def test_approved_rows_filtra_so_os_aprovados() -> None:
    pid_ok, pid_nao = "par_ok", "par_nao"
    linhas = build_funnel_table(
        [_par(pid_ok), _par(pid_nao)],
        None,
        {pid_ok: _manifesto(pid_ok, True), pid_nao: _manifesto(pid_nao, False)},
    )
    aprovados = approved_rows(linhas)
    assert [l["par"] for l in aprovados] == [pid_ok]


def test_manifests_from_response_le_a_resposta_da_ponte() -> None:
    """A tabela consegue exibir o veredicto recem-calculado pela ponte."""
    m = _manifesto("par_x", True).to_dict()
    saida = gui_core.manifests_from_response({"aprovados": [m], "reprovados": []})
    assert "par_x" in saida
    assert saida["par_x"].gate_boltz.approved is True


# --------------------------------------------------------------------------
# 3. descoberta de pares e planilha (em tmp_path, sem tocar o disco de dados)
# --------------------------------------------------------------------------
def _campanha_falsa(tmp_path: Path) -> Subproject:
    raiz = tmp_path / "resultados"
    pred = raiz / "boltz_results_PAR_A" / "predictions" / "PAR_A"
    pred.mkdir(parents=True)
    (pred / "PAR_A_model_0.cif").write_text("data_x\n", encoding="utf-8")
    (pred / "confidence_PAR_A_model_0.json").write_text("{}", encoding="utf-8")
    (pred / "pae_PAR_A_model_0.npz").write_bytes(b"")
    # segundo par, INCOMPLETO de proposito (sem o .npz)
    pred_b = raiz / "boltz_results_PAR_B" / "predictions" / "PAR_B"
    pred_b.mkdir(parents=True)
    (pred_b / "PAR_B_model_0.cif").write_text("data_x\n", encoding="utf-8")
    (pred_b / "confidence_PAR_B_model_0.json").write_text("{}", encoding="utf-8")
    return Subproject(
        key="SUBX",
        name="teste",
        campaign="SUBX",
        results_roots=[str(raiz)],
    )


def test_discover_pairs_lista_e_marca_incompletos(tmp_path) -> None:
    pares = discover_pairs(_campanha_falsa(tmp_path))
    assert [p.pair_id for p in pares] == ["PAR_A", "PAR_B"]
    assert pares[0].ok is True
    assert pares[1].ok is False
    assert any("PAE ausente" in p for p in pares[1].problems)


def test_discover_pairs_raiz_inexistente_falha_alto(tmp_path) -> None:
    sub = Subproject(
        key="SUBX", name="t", campaign="SUBX", results_roots=[str(tmp_path / "nada")]
    )
    with pytest.raises(GuiError):
        discover_pairs(sub)


def test_load_scores_tsv_le_o_formato_do_funil(tmp_path) -> None:
    """Celula vazia vira None; a coluna de veredicto manual e lida como bool."""
    tsv = tmp_path / "FUNIL_HITS.tsv"
    tsv.write_text(
        "label\tipSAE\tipTMpp\tcov_core\tface\tiface\taprovado\n"
        "PAR_A\t0.616\t0.806\t0.966\tnuclear (canonica)\t29\tTrue\n"
        "PAR_B\t\t\t\t\t\tFalse\n",
        encoding="utf-8",
    )
    sub = Subproject(
        key="SUBX",
        name="t",
        campaign="SUBX",
        results_roots=[str(tmp_path)],
        scores_tsv=str(tsv),
        scores_columns={
            "label": "label",
            "ipsae": "ipSAE",
            "iptm": "ipTMpp",
            "coverage_core_face": "cov_core",
            "face": "face",
            "n_iface_res": "iface",
        },
        manual_flag_column="aprovado",
    )
    dados = load_scores_tsv(sub)
    assert dados["PAR_A"]["ipsae"] == 0.616
    assert dados["PAR_A"]["manual"] is True
    assert dados["PAR_B"]["ipsae"] is None  # vazio != 0.0
    assert dados["PAR_B"]["face"] is None
    assert dados["PAR_B"]["manual"] is False


def test_load_scores_tsv_coluna_ausente_falha_alto(tmp_path) -> None:
    tsv = tmp_path / "x.tsv"
    tsv.write_text("label\tipSAE\nPAR_A\t0.6\n", encoding="utf-8")
    sub = Subproject(
        key="SUBX",
        name="t",
        campaign="SUBX",
        results_roots=[str(tmp_path)],
        scores_tsv=str(tsv),
        scores_columns={"label": "label", "ipsae": "ipSAE", "face": "face"},
    )
    with pytest.raises(GuiError, match="colunas ausentes"):
        load_scores_tsv(sub)


# --------------------------------------------------------------------------
# 4. config real do repo e artefatos
# --------------------------------------------------------------------------
def test_load_subprojects_le_a_campanha_versionada() -> None:
    """config/subproject.demo01.yaml (dialeto aninhado) carrega sem tocar
    em disco de dados nenhum."""
    subs = load_subprojects()
    assert subs, "nenhuma campanha declarada em config/"
    demo01 = [s for s in subs if s.key == "DEMO01"]
    assert demo01, [s.key for s in subs]
    s = demo01[0]
    assert s.results_roots
    assert s.scores_tsv and s.scores_tsv.endswith("scores.tsv")
    assert s.chain_roles["receptor"]["chain"] == "A"
    assert s.chain_roles["ligand"]["chain"] == "B"
    assert s.chain_roles["fail_if_ambiguous"] is True


def test_read_text_artifact_recusa_caminho_ausente() -> None:
    with pytest.raises(GuiError):
        read_text_artifact(None)


def test_read_text_artifact_le_e_trunca(tmp_path) -> None:
    alvo = tmp_path / "ambig.tbl"
    alvo.write_text("assign ( resid 10 and segid A )\n" * 50, encoding="utf-8")
    texto = read_text_artifact(alvo, max_bytes=100)
    assert "assign" in texto
    assert "truncado" in texto


# --------------------------------------------------------------------------
# papeis de cadeia declarados (fonte unica para o funil e para a ponte)
# --------------------------------------------------------------------------
def test_chain_roles_for_acha_a_campanha_por_key_ou_campaign() -> None:
    """A GUI/ponte pega o bloco declarado pela chave OU pelo nome da campanha."""
    from core.data import chain_roles_for

    bloco = chain_roles_for("DEMO01")
    assert bloco["receptor"]["chain"] == "A"
    assert bloco["ligand"]["chain"] == "B"
    assert bloco["fail_if_ambiguous"] is True


def test_chain_roles_for_campanha_desconhecida_falha_alto() -> None:
    """Campanha inexistente nao pode virar 'sem papeis' (cairia na posicao)."""
    from core.data import chain_roles_for

    with pytest.raises(GuiError):
        chain_roles_for("SUB99_inexistente")


# --------------------------------------------------------------------------
# 5. subprojetos.json curado (fusao) + indices ao vivo (observe_pairs)
#    So rodam quando ha um indice curado de verdade apontado pela env
#    PPI_SUBPROJECTS (skipif senao). Nenhum caminho de disco aqui: o
#    repositorio nao sabe, e nao deve saber, onde ficam os seus dados.
# --------------------------------------------------------------------------
_CURADO = gui_core.curated_subprojects_path()
_TEM_CURADO = _CURADO is not None
_motivo_sem_curado = (
    "subprojetos.json curado ausente (defina a env PPI_SUBPROJECTS com o "
    "caminho do indice) - teste de vinculo a dados reais pulado"
)


@pytest.mark.skipif(not _TEM_CURADO, reason=_motivo_sem_curado)
def test_load_subprojects_funde_o_json_curado() -> None:
    """(a) Com o subprojetos.json presente, a camada de dados enxerga as
    campanhas dele, cada uma com seus roots apontando para FORA do
    repositorio (ponteiros, nao copias)."""
    subs = load_subprojects()
    ids = {s.key for s in subs}
    assert {"DEMO02", "DEMO01", "DEMO03", "DEMO04"} <= ids, ids
    assert len(subs) >= 4
    for s in subs:
        assert s.results_roots, f"{s.key} sem roots"
        # roots do JSON curado vivem FORA do repositorio (sao dado).
        raiz_repo = str(Path(__file__).resolve().parent.parent)
        assert all(raiz_repo not in r for r in s.results_roots), (
            s.key, s.results_roots
        )


@pytest.mark.skipif(not _TEM_CURADO, reason=_motivo_sem_curado)
def test_merge_campanha_mantem_chain_roles_do_yaml() -> None:
    """(c) O YAML local COMPLEMENTA o item do JSON: a campanha conserva o
    bloco chain_roles e a planilha de scores, sem duplicar o subprojeto."""
    subs = load_subprojects()
    demo01 = [s for s in subs if s.key == "DEMO01"]
    assert len(demo01) == 1, "DEMO01 duplicado (fusao por id falhou)"
    s = demo01[0]
    # veio do YAML local (dialeto rico):
    assert s.chain_roles["receptor"]["chain"] == "A"
    assert s.chain_roles["ligand"]["chain"] == "B"
    assert s.scores_tsv and s.scores_tsv.endswith("scores.tsv")
    assert s.campaign == "DEMO01_barnase_barstar"
    # veio do JSON curado (navegacao):
    assert s.results_roots
    assert s.input_root


@pytest.mark.skipif(not _TEM_CURADO, reason=_motivo_sem_curado)
def test_observe_pairs_demo02_calcula_indices() -> None:
    """(b) observe_pairs do DEMO02 (barnase) devolve uma linha por run, com iptm/ipsae/
    classe preenchidos nos runs completos. Reusa o motor scoring_engine.score de verdade."""
    subs = load_subprojects()
    demo02 = [s for s in subs if s.key == "DEMO02"]
    if not demo02:
        pytest.skip("DEMO02 (barnase) nao declarado no subprojetos.json local")
    s = demo02[0]
    if not all(Path(r).is_dir() for r in s.results_roots):
        pytest.skip(f"roots do DEMO02 ausentes no disco: {s.results_roots}")

    pares = discover_pairs(s)
    linhas = gui_core.observe_pairs(s)

    # uma linha por run, nunca some um par em silencio
    assert len(linhas) == len(pares) > 0

    ok = [l for l in linhas if l.get("erro") is None]
    assert ok, "nenhum par pontuou - scoring_engine.score nao rodou no layout real?"
    for l in ok:
        # indices numericos presentes (ipsae pode ser 0.0, que e valor real != None)
        assert l["iptm"] is not None
        assert l["classe"] in {"HIT", "MAYBE", "weak"}
        assert l["cif"] and l["cif"].endswith("_model_0.cif")
        # todo campo declarado existe na linha
        for campo in gui_core.OBSERVE_FIELDS:
            assert campo in l

    # ipSAE depende da matriz PAE, que e escrita DEPOIS do .cif/confidence pelo
    # Boltz. Este teste le dado VIVO: se uma predicao estiver sendo gerada agora,
    # um run aparece pontuavel mas ainda sem PAE -- e exigir ipSAE de 100% dos
    # pares deixaria a suite vermelha so porque havia job rodando (aconteceu).
    # A garantia real continua de pe: a grande maioria tem ipSAE, e quem nao tem
    # precisa ter um motivo verificavel em disco (PAE ausente), nunca sumir calado.
    com_ipsae = [l for l in ok if l["ipsae"] is not None]
    sem_ipsae = [l for l in ok if l["ipsae"] is None]
    assert len(com_ipsae) >= 0.9 * len(ok), (
        f"so {len(com_ipsae)}/{len(ok)} pares tem ipSAE - alem do que a escrita "
        "concorrente explica; suspeitar de regressao no scoring_engine.score"
    )
    por_id = {p.pair_id: p for p in pares}
    for l in sem_ipsae:
        par = por_id.get(l["pair_id"])
        assert par is not None and not par.pae_npz, (
            f"{l['pair_id']} tem PAE em disco mas ficou sem ipSAE - isso e "
            "regressao, nao dado em transito"
        )


# --------------------------------------------------------------------------
# 6. Mirante de resultados: tabela de indices, graficos e viewer 3D
# --------------------------------------------------------------------------
def test_tabela_indices_ordena_e_tem_colunas(monkeypatch) -> None:
    """A tabela projeta observe_pairs nas colunas certas e ordena
    HIT > MAYBE > weak (e por iface-ipTM desc), com os runs 'erro' no fim.

    `tabela_indices` monta um DataFrame, entao depende do extra [viz].

    observe_pairs e monkeypatchado -> nao toca E: nem roda scoring_engine.score, so a
    logica de projecao/ordenacao e exercitada.
    """
    pytest.importorskip("pandas", reason="extra [viz] nao instalado")
    import math

    def _obs(pair_id, classe, iface_iptm, iptm=0.5, ipsae=0.2, erro=None):
        return {
            "pair_id": pair_id,
            "run_dir": "d",
            "cif": "x_model_0.cif" if erro is None else None,
            "classe": classe,
            "erro": erro,
            "iptm": iptm,
            "iface_iptm": iface_iptm,
            "ipsae": ipsae,
            "ptm": 0.6,
            "confidence_score": 0.7,
            "complex_plddt": 80.0,
            "pae_iface_mean": 9.0,
            "iface_plddt": 75.0,
            "bsa": 500.0,
            "n_contacts": 20,
            "n_iface_res": 15,
        }

    linhas = [
        _obs("w1", "weak", 0.30),
        _obs("h_lo", "HIT", 0.61),
        _obs("h_hi", "HIT", 0.72),
        _obs("m1", "MAYBE", 0.60),
        _obs("e1", None, None, iptm=None, ipsae=None, erro="cif ausente"),
    ]
    monkeypatch.setattr(gui_core, "observe_pairs", lambda sub, *, model=0: linhas)

    sub = Subproject(key="SX", name="t", campaign="SX", results_roots=["ignorado"])
    df = gui_core.tabela_indices(sub)

    for coluna in gui_core.TABELA_INDICES_COLUNAS:
        assert coluna in df.columns, coluna
    assert "erro" in df.columns

    # HIT (maior iface-ipTM primeiro) > MAYBE > weak > erro(classe None)
    assert list(df["par"]) == ["h_hi", "h_lo", "m1", "w1", "e1"]

    # ausencia continua None/NaN, jamais 0.0
    ultima = df.iloc[-1]
    assert ultima["erro"] == "cif ausente"
    assert ultima["ipTM"] is None or math.isnan(ultima["ipTM"])


def test_graficos_confianca_figuras_nao_vazias() -> None:
    """A partir de um DataFrame sintetico, devolve 3 go.Figure nao-vazias."""
    pd = pytest.importorskip("pandas", reason="extra [viz] nao instalado")
    go = pytest.importorskip(
        "plotly.graph_objects", reason="extra [viz] nao instalado"
    )

    df = pd.DataFrame(
        [
            {"par": "h1", "classe": "HIT", "ipTM": 0.90, "iface_ipTM": 0.72,
             "ipSAE": 0.80, "pTM": 0.81, "conf": 0.92, "pLDDT": 92.0,
             "PAE_iface": 8.0, "BSA": 1500.0, "n_contatos": 50, "erro": None},
            {"par": "m1", "classe": "MAYBE", "ipTM": 0.70, "iface_ipTM": 0.61,
             "ipSAE": 0.40, "pTM": 0.72, "conf": 0.74, "pLDDT": 86.0,
             "PAE_iface": 10.0, "BSA": 900.0, "n_contatos": 30, "erro": None},
            {"par": "w1", "classe": "weak", "ipTM": 0.42, "iface_ipTM": 0.30,
             "ipSAE": 0.11, "pTM": 0.55, "conf": 0.45, "pLDDT": 71.0,
             "PAE_iface": 15.0, "BSA": 120.0, "n_contatos": 6, "erro": None},
        ]
    )
    figs = gui_core.graficos_confianca(df)
    assert set(figs) == {"barras_iface_iptm", "dispersao_iptm_pae", "barras_ipsae"}
    for nome, fig in figs.items():
        assert isinstance(fig, go.Figure), nome
        assert len(fig.data) > 0, f"figura vazia: {nome}"


def test_structure_view_html_gera_viewer(cif_sintetico) -> None:
    """Viewer 3D a partir de um .cif sintetico: HTML nao-vazio com 3Dmol/viewer."""
    pytest.importorskip("py3Dmol", reason="extra [viz] nao instalado")
    from core.data import structure_view_html

    html = structure_view_html(cif_sintetico)
    assert html
    low = html.lower()
    assert "viewer" in low
    assert "3dmol" in low


def test_structure_view_html_com_iface_destaca(cif_sintetico) -> None:
    """Passar a interface (sticks) nao quebra e ainda produz o viewer."""
    pytest.importorskip("py3Dmol", reason="extra [viz] nao instalado")
    from core.data import structure_view_html

    iface = {
        "bait_chains": ["A"],
        "partner_chain": "B",
        "bait_residues": [10, 11],
        "partner_residues": [30, 31],
        "chain_sizes": {"A": 5, "B": 4},
        "hetero_chains": [],
        "cutoff": 5.0,
    }
    html = structure_view_html(cif_sintetico, iface=iface, altura=300)
    assert "viewer" in html.lower()


def test_structure_view_html_cif_ausente_falha_alto(tmp_path) -> None:
    """Sem o .cif em disco, erro claro - nunca um viewer vazio."""
    pytest.importorskip("py3Dmol", reason="extra [viz] nao instalado")
    from core.data import structure_view_html

    with pytest.raises(GuiError):
        structure_view_html(tmp_path / "nao_existe.cif")


@pytest.mark.skipif(not _TEM_CURADO, reason=_motivo_sem_curado)
def test_tabela_indices_reusa_motor_real() -> None:
    """A tabela tem as colunas certas, uma linha por run e ao menos um par
    classificado - prova de que reusa o scoring_engine.score de verdade sobre
    dados em disco. Pulado sem um indice curado declarado (ver PPI_SUBPROJECTS)
    e sem o extra [viz]. Usa a MENOR campanha disponivel, para nao re-scorar
    uma campanha grande - a cobertura do motor real
    ja e
    feita, mais a fundo, por test_observe_pairs_demo02_calcula_indices."""
    pytest.importorskip("pandas", reason="extra [viz] nao instalado")
    subs = load_subprojects()
    ordem_preferida = ["DEMO03", "DEMO01", "DEMO02", "DEMO04"]
    candidatos = sorted(
        subs,
        key=lambda s: ordem_preferida.index(s.key) if s.key in ordem_preferida else 99,
    )
    alvo = None
    for s in candidatos:
        if all(Path(r).is_dir() for r in s.results_roots):
            alvo = s
            break
    if alvo is None:
        pytest.skip("nenhum subprojeto com roots presentes no disco")

    df = gui_core.tabela_indices(alvo)
    for coluna in gui_core.TABELA_INDICES_COLUNAS:
        assert coluna in df.columns
    assert "erro" in df.columns
    assert len(df) == len(discover_pairs(alvo)) > 0
    assert df["classe"].isin(["HIT", "MAYBE", "weak"]).any()


def test_toolchain_status_reconhece_binario_ou_imagem() -> None:
    """A GUI reconhece o HADDOCK3 por binario local OU pela imagem Docker.

    Antes so olhava o binario local (que nunca existe no Windows) e dizia "nao
    instalado" mesmo com a imagem ppi-haddock3 pronta. Agora available reflete os
    dois motores, como o run() do adaptador faz.
    """
    from core.data import haddock3_toolchain_status

    estado = haddock3_toolchain_status()
    assert isinstance(estado["available"], bool)
    # novas chaves: cada motor reportado separadamente
    assert "local" in estado and isinstance(estado["local"]["available"], bool)
    assert "docker" in estado and isinstance(estado["docker"]["available"], bool)
    assert estado["motor"] in {"local", "docker", "nenhum"}
    # se a imagem Docker existe, available tem de ser True mesmo sem binario local
    if estado["docker"]["available"]:
        assert estado["available"] is True
        if not estado["local"]["available"]:
            assert estado["motor"] == "docker"
    assert estado["via"] in {
        "funnel.haddock_adapter.check_toolchain",
        "funnel.haddock_docker.check_image",
        "shutil.which (fallback)",
    }


def test_check_image_separa_daemon_parado_de_imagem_ausente(monkeypatch) -> None:
    """Docker fechado nao pode ser relatado como "imagem ausente".

    Com o Docker Desktop fechado, `docker image inspect` falha com "failed to
    connect to the docker API" -- e o diagnostico mandava CONSTRUIR a imagem,
    conselho caro e errado (a imagem ja existe; o que falta e subir o servico).
    """
    import subprocess as _sub

    from funnel import haddock_docker

    class _Proc:
        def __init__(self, err: str) -> None:
            self.returncode = 1
            self.stdout = ""
            self.stderr = err

    daemon_fora = (
        "failed to connect to the docker API at npipe:////./pipe/"
        "dockerDesktopLinuxEngine; check if the daemon is running"
    )
    monkeypatch.setattr(_sub, "run", lambda *a, **k: _Proc(daemon_fora))
    fora = haddock_docker.check_image()
    assert fora["available"] is False
    assert fora["motivo"] == "daemon_parado"
    assert "docker build" not in fora["error"].lower()

    monkeypatch.setattr(
        _sub, "run", lambda *a, **k: _Proc("Error: No such image: ppi-haddock3:latest")
    )
    ausente = haddock_docker.check_image()
    assert ausente["motivo"] == "imagem_ausente"
    assert "docker build" in ausente["error"]
