# -*- coding: utf-8 -*-
"""Caminho de PRODUCAO do funil: `funnel.scoring` + `funnel.pipeline`.

O que estes testes travam:

* o manifesto sai com os papeis BIOLOGICOS corretos (bait = isca/receptor,
  partner = parceiro/ligante) e com os caminhos reais do run em disco;
* celula vazia da planilha vira ``None``, **nunca 0.0** (um 0.0 inventado
  passaria pelo gate como "medido e ruim" em vez de "nao medido");
* o ipTM consumido e o ``protein_iptm`` do run, nao o ``iptm`` global (que
  inclui os ligantes/cofatores da predicao e infla);
* label fora da planilha e ``.cif`` ausente falham ALTO, e o lote inteiro
  continua reportando a contagem real (falha vira linha de erro, nao sumico);
* ``run_funnel`` e idempotente e escreve SO na raiz de saida pedida.

Tudo roda OFFLINE sobre o `.cif` sintetico da suite (fixture
``cif_sintetico``) e uma planilha de scores gerada no `tmp_path`: a suite nao
depende de disco de dados nenhum. Os testes que exigem o repositorio dos
engines de docking sao PULADOS quando ele nao esta presente - nunca verdes em
silencio.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from funnel import pipeline, scoring
from funnel.contract import PairManifest
from funnel.haddock_adapter import docking_suite_home
from funnel.scoring import ScoringError

REPO_ROOT = Path(__file__).resolve().parent.parent

_sem_engines = pytest.mark.skipif(
    not (docking_suite_home() / "engines" / "__init__.py").is_file(),
    reason=f"repo dos engines ausente em {docking_suite_home()}",
)
# Cabecalho da planilha LONG de scores: as duas primeiras colunas sao as de
# IDENTIDADE do par (bait/partner), e e isso que a campanha sintetica mapeia
# em `scores.bait_column`/`scores.partner_column`.
_CABECALHO = (
    "bait\tpartner\tlabel\tipSAE\tipTMpp\tiface\tcore_face\tcov_core\talt_face\t"
    "face\tpdockq_ctx\taprovado"
)


# --------------------------------------------------------------------------
# campanha SINTETICA (nao toca em E:)
# --------------------------------------------------------------------------
def _linha(label: str, ipsae: str, iptm: str, iface: str, cov: str, face: str) -> str:
    """Uma linha da planilha no formato LONG do funil."""
    return (
        f"IscaSint\tParceiroSint\t{label}\t{ipsae}\t{iptm}\t{iface}\t{iface}\t{cov}\t0\t"
        f"{face}\t\tTrue"
    )


@pytest.fixture()
def campanha(tmp_path, cif_sintetico) -> dict:
    """Config de campanha sintetica, com runs de mentira no disco temporario.

    Tres pares na planilha:

    * ``PAR_OK``    - metricas boas e run completo em disco (aprova no GATE 1);
    * ``PAR_VAZIO`` - celulas VAZIAS (ausencia, nao zero) e run completo;
    * ``PAR_SEM_RUN`` - esta na planilha mas nao tem run nenhum em disco.
    """
    resultados = tmp_path / "resultados"
    avaliacoes = tmp_path / "avaliacoes"
    avaliacoes.mkdir(parents=True)

    for label in ("PAR_OK", "PAR_VAZIO"):
        pred = resultados / f"boltz_results_{label}" / "predictions" / label
        pred.mkdir(parents=True)
        shutil.copy(cif_sintetico, pred / f"{label}_model_0.cif")
        (pred / f"confidence_{label}_model_0.json").write_text(
            json.dumps(
                {
                    "confidence_score": 0.9,
                    "ptm": 0.8,
                    "iptm": 0.99,            # global: INCLUI cofator, nao usar
                    "protein_iptm": 0.62,    # e este que deve entrar no Scores
                    "complex_plddt": 0.8945,  # escala 0-1 do Boltz
                }
            ),
            encoding="utf-8",
        )

    (avaliacoes / "FUNIL.tsv").write_text(
        "\n".join(
            [
                _CABECALHO,
                # ipTMpp VAZIO de proposito: assim o unico ipTM possivel e o
                # `protein_iptm` do JSON do run (e nao o `iptm` global de la).
                _linha("PAR_OK", "0.60", "", "9", "1.0", "nuclear (canonica)"),
                _linha("PAR_VAZIO", "", "", "", "", ""),
                _linha("PAR_SEM_RUN", "0.60", "", "9", "1.0", "nuclear (canonica)"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    return {
        "id": "SUBTEST",
        "label": "Sintetico",
        "campaign": "SUBTEST_sintetico",
        "paths": {
            "results_root": str(resultados),
            "avaliacoes": str(avaliacoes),
            "haddock_out": str(tmp_path / "haddock"),
        },
        "scores": {
            "tsv": "FUNIL.tsv",
            "iptm_field": "protein_iptm",
            "bait_column": "bait",
            "partner_column": "partner",
        },
        # o `.cif` sintetico tem cadeia A com 5 residuos e B com 4; a campanha
        # declara a MENOR como receptor DE PROPOSITO, para o teste exercitar o
        # caso em que a ordem das cadeias engana (que e o caso realista).
        "chain_roles": {
            "receptor": {"molecule": "isca", "chain": "B", "n_residues_esperado": 4},
            "ligand": {"molecule": "parceiro", "chain": "A", "n_residues_esperado_min": 5},
            "fail_if_ambiguous": True,
        },
    }


# --------------------------------------------------------------------------
# 1. resolucao da campanha
# --------------------------------------------------------------------------
def test_resolve_subproject_aceita_id_e_campaign() -> None:
    """A mesma campanha e chamada de 'DEMO01' (n8n) e 'DEMO01_barnase_barstar' (ponte)."""
    por_id = scoring.resolve_subproject("DEMO01")
    por_campanha = scoring.resolve_subproject("DEMO01_barnase_barstar")
    assert por_id.key == por_campanha.key == "DEMO01"
    assert por_id.campaign == "DEMO01_barnase_barstar"


def test_resolve_subproject_desconhecido_lista_as_conhecidas() -> None:
    """Campanha inexistente = erro claro, nunca a campanha 'mais parecida'."""
    with pytest.raises(ScoringError) as exc:
        scoring.resolve_subproject("SUB99_inexistente")
    assert "DEMO01" in str(exc.value)


# --------------------------------------------------------------------------
# 2. build_manifest
# --------------------------------------------------------------------------
def test_build_manifest_preenche_papeis_e_caminhos(campanha) -> None:
    """bait/partner vem das colunas de identidade; cif/run_dir, do disco."""
    m = scoring.build_manifest("PAR_OK", campanha)
    assert isinstance(m, PairManifest)
    assert m.campaign == "SUBTEST_sintetico"
    assert (m.bait, m.partner) == ("IscaSint", "ParceiroSint")
    assert m.stage == "scored"
    assert Path(m.cif_model).is_file()
    assert Path(m.boltz_run_dir).is_dir()
    assert m.scores.ipsae == pytest.approx(0.60)
    assert m.scores.coverage_core_face == pytest.approx(1.0)
    assert m.scores.face == "nuclear (canonica)"
    assert m.scores.n_iface_res == 9


def test_iptm_vem_do_protein_iptm_e_nao_do_global(campanha) -> None:
    """O `iptm` global inclui cofatores e infla: o funil consome `protein_iptm`."""
    m = scoring.build_manifest("PAR_OK", campanha)
    assert m.scores.iptm == pytest.approx(0.62)
    # pLDDT segue a escala do motor (`scoring_engine.score`): 0-1 do Boltz vira 0-100
    assert m.scores.complex_plddt == pytest.approx(89.45, abs=0.01)


def test_celula_vazia_vira_none_nunca_zero(campanha) -> None:
    """Ausencia e ausencia: 0.0 seria 'medido e ruim', que e outra coisa."""
    m = scoring.build_manifest("PAR_VAZIO", campanha)
    assert m.scores.ipsae is None
    assert m.scores.coverage_core_face is None
    assert m.scores.face is None
    # metricas nao recalculadas aqui continuam explicitamente ausentes
    assert m.scores.pae_iface_mean is None
    assert m.scores.bsa is None
    assert m.scores.n_contacts == 0


def test_build_manifest_label_fora_da_planilha_falha_alto(campanha) -> None:
    with pytest.raises(ScoringError) as exc:
        scoring.build_manifest("NAO_EXISTE", campanha)
    assert "planilha" in str(exc.value)


def test_build_manifest_sem_run_em_disco_falha_alto(campanha) -> None:
    """Sem `.cif` nao ha manifesto: nada de envelope meia-boca em silencio."""
    with pytest.raises(ScoringError):
        scoring.build_manifest("PAR_SEM_RUN", campanha)


# --------------------------------------------------------------------------
# 3. build_all / build_all_report
# --------------------------------------------------------------------------
def test_build_all_report_separa_ok_de_falha(campanha) -> None:
    manifestos, falhas = scoring.build_all_report(campanha)
    assert [m.pair_id for m in manifestos] == ["PAR_OK", "PAR_VAZIO"]
    assert [f["pair_id"] for f in falhas] == ["PAR_SEM_RUN"]


def test_build_all_estrito_falha_quando_um_par_nao_monta(campanha) -> None:
    """strict=True nao devolve um lote menor do que a planilha em silencio."""
    with pytest.raises(ScoringError) as exc:
        scoring.build_all(campanha)
    assert "PAR_SEM_RUN" in str(exc.value)
    assert len(scoring.build_all(campanha, strict=False)) == 2


def test_score_pair_devolve_o_bloco_de_scores(campanha) -> None:
    """Contrato de POST /score com pair_id: a ponte publica isto como 'scores'."""
    sc = scoring.score_pair(subproject=campanha, pair_id="PAR_OK")
    assert sc.to_dict()["ipsae"] == pytest.approx(0.60)


# --------------------------------------------------------------------------
# 4. pipeline
# --------------------------------------------------------------------------
def test_ate_invalido_falha_alto(campanha) -> None:
    with pytest.raises(pipeline.PipelineError):
        pipeline.run_funnel(campanha, ate="voando")


def test_run_funnel_ate_gate_nao_prepara_nada(campanha, tmp_path) -> None:
    """`ate='gate'` para no portao: veredicto gravado, nenhum .cfg gerado."""
    saida = tmp_path / "saida_gate"
    resumo = pipeline.run_funnel(campanha, ate="gate", out_root=saida)
    assert resumo["n_total"] == 3          # 3 linhas na planilha
    assert resumo["n_manifestos"] == 2     # 1 sem run em disco
    assert resumo["n_erros"] == 1
    assert resumo["n_aprovados"] == 1 and resumo["n_reprovados"] == 1
    assert resumo["artefatos"] == []
    assert [r["label"] for r in resumo["reprovados_resumo"]] == ["PAR_VAZIO"]
    # o manifesto de CADA par avaliado fica em disco, aprovado ou nao
    manifestos = sorted(p.name for p in (saida / "manifests").glob("*.json"))
    assert manifestos == ["PAR_OK.json", "PAR_VAZIO.json"]
    assert not list(saida.rglob("*.cfg"))


def test_run_funnel_nao_escreve_fora_da_raiz_pedida(campanha, tmp_path) -> None:
    """Raiz explicita manda: nada vaza para o haddock_out declarado no YAML."""
    saida = tmp_path / "outro_destino"
    pipeline.run_funnel(campanha, ate="gate", out_root=saida)
    assert not Path(campanha["paths"]["haddock_out"]).exists()


@_sem_engines
def test_run_funnel_prepare_gera_cfg_e_tbl_do_aprovado(campanha, tmp_path) -> None:
    """O aprovado sai com .cfg + ambig.tbl bidirecional e os PDBs do split."""
    saida = tmp_path / "saida_prep"
    resumo = pipeline.run_funnel(campanha, ate="prepare", out_root=saida)
    assert resumo["n_aprovados"] == 1
    art = resumo["artefatos"][0]
    assert art["label"] == "PAR_OK"
    cfg, tbl = Path(art["cfg"]), Path(art["ambig_tbl"])
    assert cfg.is_file() and tbl.is_file() and tbl.stat().st_size > 0
    texto_cfg = cfg.read_text(encoding="utf-8")
    assert "reference_fname" in texto_cfg          # caprieval contra o .cif
    assert "ligand_param_fname" not in texto_cfg   # protein_protein=True
    linhas = [l for l in tbl.read_text(encoding="utf-8").splitlines() if l.startswith("assign")]
    assert [l for l in linhas if "segid A" in l] and [l for l in linhas if "segid C" in l]
    # receptor = a cadeia DECLARADA (a menor), nao a primeira do arquivo
    assert art["n_ativos_bait"] > 0 and art["n_ativos_partner"] > 0
    assert art["papeis_fonte"] == "declarado"


@_sem_engines
def test_run_funnel_e_idempotente(campanha, tmp_path) -> None:
    """Rodar duas vezes reescreve o mesmo conteudo - nao duplica nem corrompe."""
    saida = tmp_path / "saida_idem"
    primeiro = pipeline.run_funnel(campanha, ate="prepare", out_root=saida)
    antes = {
        p.relative_to(saida).as_posix(): p.read_bytes()
        for p in sorted(saida.rglob("*"))
        if p.is_file()
    }
    segundo = pipeline.run_funnel(campanha, ate="prepare", out_root=saida)
    depois = {
        p.relative_to(saida).as_posix(): p.read_bytes()
        for p in sorted(saida.rglob("*"))
        if p.is_file()
    }
    assert set(antes) == set(depois)
    assert antes == depois
    assert primeiro["n_aprovados"] == segundo["n_aprovados"] == 1
