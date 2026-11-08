# -*- coding: utf-8 -*-
"""Contrato de dados: o envelope tem de sobreviver ao round-trip disco->memoria.

O `PairManifest` e o que trafega entre os nos do n8n e entre os estagios do
funil. Se um campo se perder no save/load, o estagio seguinte trabalha com dado
faltante sem perceber - exatamente a degradacao silenciosa que o projeto proibe.
"""

from __future__ import annotations

import json

import pytest

from funnel.contract import (
    GateResult,
    HaddockArtifacts,
    PairManifest,
    Scores,
    load_manifest,
    save_manifest,
)


def _manifesto_completo() -> PairManifest:
    """Manifesto com TODOS os campos preenchidos (nada em default)."""
    return PairManifest(
        campaign="DEMO01_barnase_barstar",
        pair_id="barnase_x_barstar",
        bait="barnase",
        partner="barstar",
        boltz_run_dir="/dados/demo01/resultados/boltz_results_x",
        cif_model="/dados/demo01/resultados/barnase_x_barstar_model_0.cif",
        scores=Scores(
            iptm=0.8118,
            iface_iptm=0.8062894940376282,
            ipsae=0.616,
            pae_iface_mean=4.21,
            complex_plddt=88.4,
            bsa=1234.5,
            n_contacts=29,
            n_iface_res=29,
            coverage_core_face=0.966,
            face="nuclear (canonica)",
        ),
        gate_boltz=GateResult(
            approved=True,
            reasons=["ipSAE 0.616 >= 0.15", "VEREDICTO: aprovado para HADDOCK3"],
            criteria={"ipsae_min": 0.15, "coverage_min": 0.5, "require_canonical_face": True},
        ),
        receptor_pdb="/work/pdbs/receptor.pdb",
        ligand_pdb="/work/pdbs/ligand.pdb",
        active_residues=[12, 15, 143, 144, 341],
        partner_active_residues=[530, 531, 532],
        haddock=HaddockArtifacts(
            cfg="/work/run-barnase-barstar.cfg",
            ambig_tbl="/work/ambig.tbl",
            run_dir="/work/run-barnase-barstar",
            capri_clt="/work/run/analysis/capri_clt.tsv",
            decision="aprovado no GATE 2",
        ),
        stage="prepared",
    )


def test_round_trip_preserva_tudo(tmp_path) -> None:
    """save_manifest -> load_manifest devolve um envelope IDENTICO."""
    original = _manifesto_completo()
    destino = save_manifest(original, tmp_path / "manifests" / "par.json")
    assert destino.is_file()

    recarregado = load_manifest(destino)
    assert recarregado.to_dict() == original.to_dict()


def test_round_trip_preserva_campos_aninhados(tmp_path) -> None:
    """Scores/GateResult/HaddockArtifacts voltam como dataclasses, nao dicts."""
    destino = save_manifest(_manifesto_completo(), tmp_path / "par.json")
    m = load_manifest(destino)
    assert isinstance(m.scores, Scores)
    assert isinstance(m.gate_boltz, GateResult)
    assert isinstance(m.haddock, HaddockArtifacts)
    assert m.scores.ipsae == 0.616
    assert m.gate_boltz.approved is True
    assert m.gate_boltz.criteria["ipsae_min"] == 0.15
    assert m.haddock.capri_clt.endswith("capri_clt.tsv")
    assert m.active_residues == [12, 15, 143, 144, 341]
    assert m.partner_active_residues == [530, 531, 532]
    assert m.stage == "prepared"


def test_manifesto_gravado_e_json_puro(tmp_path) -> None:
    """O arquivo tem de ser JSON legivel por qualquer no do n8n."""
    destino = save_manifest(_manifesto_completo(), tmp_path / "par.json")
    dados = json.loads(destino.read_text(encoding="utf-8"))
    assert dados["pair_id"] == "barnase_x_barstar"
    assert dados["scores"]["face"] == "nuclear (canonica)"
    assert dados["gate_boltz"]["approved"] is True


def test_manifesto_minimo_tambem_faz_round_trip(tmp_path) -> None:
    """Envelope recem-criado (so identidade) sobrevive igualmente."""
    m = PairManifest(
        campaign="DEMO01_barnase_barstar", pair_id="par", bait="isca", partner="parceiro"
    )
    destino = save_manifest(m, tmp_path / "min.json")
    assert load_manifest(destino).to_dict() == m.to_dict()


def test_ausencia_nunca_vira_zero() -> None:
    """Celula vazia da planilha vira None, jamais 0.0."""
    s = Scores.from_dict({"ipsae": "", "iptm": "None", "coverage_core_face": "nan"})
    assert s.ipsae is None
    assert s.iptm is None
    assert s.coverage_core_face is None


def test_campos_extras_sao_ignorados() -> None:
    """from_dict tolera campo desconhecido (o n8n pode carregar metadado extra)."""
    m = PairManifest.from_dict(
        {
            "campaign": "c",
            "pair_id": "p",
            "bait": "b",
            "partner": "x",
            "campo_que_nao_existe": 42,
        }
    )
    assert m.pair_id == "p"


def test_identidade_incompleta_falha_alto() -> None:
    """Sem pair_id nao ha envelope - erro claro, nao um manifesto meia-boca."""
    with pytest.raises(ValueError):
        PairManifest.from_dict({"campaign": "c", "bait": "b", "partner": "x"})


def test_stage_invalido_falha_alto() -> None:
    """Estagio fora de STAGES quebra o fluxo do funil - tem de explodir."""
    with pytest.raises(ValueError):
        PairManifest(
            campaign="c", pair_id="p", bait="b", partner="x", stage="quase_pronto"
        )


def test_valor_nao_numerico_falha_alto() -> None:
    """Lixo numerico nao vira 0.0 silencioso."""
    with pytest.raises(TypeError):
        Scores.from_dict({"ipsae": "abc"})


def test_manifesto_inexistente_falha_alto(tmp_path) -> None:
    """load_manifest de arquivo ausente e erro, nao envelope vazio."""
    with pytest.raises(FileNotFoundError):
        load_manifest(tmp_path / "nao_existe.json")


def test_manifesto_corrompido_falha_alto(tmp_path) -> None:
    """JSON malformado nao pode virar manifesto default."""
    ruim = tmp_path / "ruim.json"
    ruim.write_text("{isso nao e json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifest(ruim)
