# -*- coding: utf-8 -*-
"""GATE 1/GATE 2 - o teste mais importante da suite.

O objetivo central: **o veredicto do gate codificado tem de bater, par a par,
com um veredicto declarado DE FORA** -- nao com o que o proprio gate calcula.

Fonte dos dados
---------------
A tabela `_TABELA_SINTETICA` abaixo e SINTETICA e construida para cobrir os
ramos de decisao do GATE 1: cada linha isola um criterio (ipSAE no limite,
cobertura abaixo do piso, face atipica, celula vazia, metrica secundaria
ausente...) e traz, na coluna `aprovado`, o veredicto esperado derivado A MAO
dos limiares de ``config/funnel.yaml``.

Nenhum numero aqui e medida de nada: sao casos de teste. Este repositorio nao
contem resultado de pesquisa, e a suite nao depende de disco algum para rodar.

Limiares em vigor (``config/funnel.yaml``), para conferir a coluna `aprovado`:

    DECISIVOS    ipSAE >= 0.15 E cobertura da face nuclear >= 0.50
                 E face na lista ["nuclear (canonica)"]
    SECUNDARIOS  ipTM >= 0.50, PAE iface <= 10.0 A, contatos >= 10
                 -- reprovam quando PRESENTES e fora do limiar; quando
                 AUSENTES nao bloqueiam (strict_missing=false)

A leitura da tabela monta `Scores` com ipsae/iptm/cobertura/face/n_iface_res;
PAE e n_contacts ficam ausentes de proposito, para o caminho
"secundario ausente nao bloqueia" ser exercitado em TODAS as linhas.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from funnel.contract import Scores
from funnel.gate import DEFAULT_CRITERIA, gate_boltz, gate_haddock, load_criteria

# Tabela SINTETICA de casos de teste (nao e medida de nada - ver a docstring).
# Colunas iguais as de uma planilha LONG do funil, para exercitar tambem o
# formato: bait, partner, label, ipSAE, ipTMpp, iface, core_face, cov_core,
# alt_face, face, pdockq_ctx, aprovado.
_TABELA_SINTETICA = """\
bait\tpartner\tlabel\tipSAE\tipTMpp\tiface\tcore_face\tcov_core\talt_face\tface\tpdockq_ctx\taprovado
isca\tp01\tCASO_01_tudo_passa\t0.62\t0.81\t29\t28\t0.97\t0\tnuclear (canonica)\t0.24\tTrue
isca\tp02\tCASO_02_folga_menor\t0.30\t0.70\t24\t24\t1.0\t0\tnuclear (canonica)\t0.31\tTrue
isca\tp03\tCASO_03_no_limite_exato\t0.15\t0.50\t20\t11\t0.50\t0\tnuclear (canonica)\t0.20\tTrue
isca\tp04\tCASO_04_secundaria_ausente\t0.60\t\t22\t21\t0.95\t0\tnuclear (canonica)\t0.28\tTrue
isca\tp05\tCASO_05_ipsae_logo_abaixo\t0.14\t0.80\t26\t25\t0.96\t0\tnuclear (canonica)\t0.30\tFalse
isca\tp06\tCASO_06_cobertura_abaixo\t0.60\t0.80\t43\t19\t0.45\t7\tnuclear (canonica)\t0.25\tFalse
isca\tp07\tCASO_07_face_atipica\t0.60\t0.80\t41\t37\t0.90\t4\tperiferica (atipica)\t0.22\tFalse
isca\tp08\tCASO_08_iptm_logo_abaixo\t0.60\t0.49\t25\t24\t0.96\t0\tnuclear (canonica)\t0.15\tFalse
isca\tp09\tCASO_09_ipsae_ausente\t\t0.80\t30\t29\t0.97\t0\tnuclear (canonica)\t0.33\tFalse
isca\tp10\tCASO_10_cobertura_ausente\t0.60\t0.80\t30\t29\t\t0\tnuclear (canonica)\t0.33\tFalse
isca\tp11\tCASO_11_face_vazia\t0.60\t0.80\t30\t25\t0.83\t0\t\t\tFalse
isca\tp12\tCASO_12_tudo_vazio\t\t\t\t\t\t\t\t\tFalse
isca\tp13\tCASO_13_iptm_zero\t0.60\t0.0\t28\t27\t0.96\t0\tnuclear (canonica)\t\tFalse
isca\tp14\tCASO_14_sem_interface\t0.20\t0.55\t0\t0\t0.0\t0\tsem interface\t0.0\tFalse
"""

#: Os casos cuja coluna `aprovado` e True (derivada a mao dos limiares).
APROVADOS_ESPERADOS = (
    "CASO_01_tudo_passa",
    "CASO_02_folga_menor",
    "CASO_03_no_limite_exato",
    "CASO_04_secundaria_ausente",
)


# --------------------------------------------------------------------------
# leitura da planilha -> Scores (celula vazia = None, NUNCA 0.0)
# --------------------------------------------------------------------------
def _celula(valor: "str | None") -> "str | None":
    """Celula do TSV: vazia/'None'/'nan' viram None."""
    if valor is None:
        return None
    texto = valor.strip()
    if texto == "" or texto.lower() in {"none", "nan", "na"}:
        return None
    return texto


def _linhas_funil() -> "List[Dict[str, Any]]":
    """Le a tabela sintetica de casos (nenhum acesso a disco)."""
    texto = _TABELA_SINTETICA
    linhas = [l for l in texto.splitlines() if l.strip()]
    cabecalho = linhas[0].split("\t")
    fora: "List[Dict[str, Any]]" = []
    for linha in linhas[1:]:
        celulas = linha.split("\t")
        # split() nao devolve as colunas finais vazias -> completa explicitamente
        celulas += [""] * (len(cabecalho) - len(celulas))
        fora.append(dict(zip(cabecalho, celulas)))
    return fora


def _scores_da_linha(linha: "Dict[str, Any]") -> Scores:
    """Traduz uma linha de FUNIL_HITS.tsv para o contrato `Scores`.

    ``ipTMpp`` e o ``protein_iptm`` (sem ATP/MG) - o ipTM global infla e nao
    deve ser usado, conforme a config da campanha.
    """
    return Scores(
        ipsae=_como_float(linha["ipSAE"]),
        iptm=_como_float(linha["ipTMpp"]),
        coverage_core_face=_como_float(linha["cov_core"]),
        face=_celula(linha["face"]),
        n_iface_res=int(float(linha["iface"])) if _celula(linha["iface"]) else 0,
    )


def _como_float(valor: "str | None") -> "float | None":
    texto = _celula(valor)
    return None if texto is None else float(texto)


@pytest.fixture(scope="module")
def pares() -> "List[Dict[str, Any]]":
    """Os casos de teste, ja traduzidos para `Scores`."""
    return [
        {"label": l["label"], "manual": l["aprovado"].strip() == "True", "scores": _scores_da_linha(l)}
        for l in _linhas_funil()
    ]


@pytest.fixture(scope="module")
def criterios() -> "Dict[str, Any]":
    """Limiares REAIS do repo (config/funnel.yaml), nao os embutidos."""
    return load_criteria()


# --------------------------------------------------------------------------
# 1. reproducao EXATA do funil manual
# --------------------------------------------------------------------------
def test_tabela_cobre_aprovacao_e_reprovacao(pares) -> None:
    """Sanidade da base do teste: a tabela tem casos dos dois lados."""
    assert len(pares) == 14
    aprovados = [p["label"] for p in pares if p["manual"]]
    assert sorted(aprovados) == sorted(APROVADOS_ESPERADOS)
    assert len(pares) - len(aprovados) == 10


def test_gate_reproduz_o_veredicto_declarado(pares, criterios) -> None:
    """GATE 1 aprova EXATAMENTE os casos marcados True, e nenhum outro."""
    aprovados = [p["label"] for p in pares if gate_boltz(p["scores"], criterios).approved]
    reprovados = [p["label"] for p in pares if not gate_boltz(p["scores"], criterios).approved]

    assert len(aprovados) == 4, f"esperava 4 aprovados, veio {len(aprovados)}: {aprovados}"
    assert len(reprovados) == 10
    assert sorted(aprovados) == sorted(APROVADOS_ESPERADOS)


def test_gate_concorda_caso_a_caso(pares, criterios) -> None:
    """Nenhuma divergencia individual (nao basta a contagem bater)."""
    divergencias = [
        (p["label"], p["manual"], gate_boltz(p["scores"], criterios).approved)
        for p in pares
        if gate_boltz(p["scores"], criterios).approved != p["manual"]
    ]
    assert not divergencias, f"gate divergiu do veredicto declarado em: {divergencias}"


def test_gate_com_defaults_embutidos_da_o_mesmo_resultado(pares) -> None:
    """Sem YAML (defaults de DEFAULT_CRITERIA) o veredicto e exatamente o mesmo."""
    aprovados = [
        p["label"] for p in pares if gate_boltz(p["scores"], DEFAULT_CRITERIA).approved
    ]
    assert sorted(aprovados) == sorted(APROVADOS_ESPERADOS)


# --------------------------------------------------------------------------
# 2. ausencia NAO aprova por omissao
# --------------------------------------------------------------------------
def _hit_base() -> Scores:
    """Caso aprovado de referencia (CASO_01), como ponto de partida."""
    return Scores(
        ipsae=0.62,
        iptm=0.81,
        coverage_core_face=0.97,
        face="nuclear (canonica)",
        n_iface_res=29,
    )


def test_hit_base_realmente_aprova(criterios) -> None:
    """Sanidade: sem mexer em nada, o HIT de referencia passa."""
    assert gate_boltz(_hit_base(), criterios).approved is True


@pytest.mark.parametrize("campo", ["ipsae", "coverage_core_face", "face"])
def test_campo_decisivo_faltante_nao_aprova(campo, criterios) -> None:
    """Criterio decisivo ausente reprova e aparece como FALTANTE nos motivos."""
    sc = _hit_base()
    setattr(sc, campo, None)
    r = gate_boltz(sc, criterios)
    assert r.approved is False
    assert any("FALTANTE" in m for m in r.reasons), r.reasons


def test_scores_completamente_vazio_nao_aprova(criterios) -> None:
    """Um par sem metrica nenhuma nao pode passar por omissao."""
    r = gate_boltz(Scores(), criterios)
    assert r.approved is False
    assert "VEREDICTO: reprovado" in r.reasons[-1]


def test_face_atipica_reprova(criterios) -> None:
    """Face 'periferica (atipica)' reprova mesmo com ipSAE/cobertura altos."""
    sc = _hit_base()
    sc.face = "periferica (atipica)"
    r = gate_boltz(sc, criterios)
    assert r.approved is False
    assert any("periferica (atipica)" in m for m in r.reasons)


def test_face_vazia_reprova(criterios) -> None:
    """Celula de face vazia (CASO_11/CASO_12) nao vale como canonica."""
    sc = _hit_base()
    sc.face = "   "
    r = gate_boltz(sc, criterios).reasons
    assert any("face FALTANTE" in m for m in r)


def test_cobertura_abaixo_do_limiar_reprova(criterios) -> None:
    """Cobertura abaixo do piso (CASO_06) reprova."""
    sc = _hit_base()
    sc.coverage_core_face = 0.45
    r = gate_boltz(sc, criterios)
    assert r.approved is False
    assert any("0.450" in m for m in r.reasons)


# --------------------------------------------------------------------------
# 3. os motivos trazem o valor REAL observado (veredicto auditavel)
# --------------------------------------------------------------------------
def test_reasons_trazem_os_valores_reais(criterios) -> None:
    """Motivos citam ipSAE, cobertura e face com os numeros do par."""
    r = gate_boltz(_hit_base(), criterios)
    texto = " | ".join(r.reasons)
    assert "0.620" in texto
    assert "0.970" in texto
    assert "nuclear (canonica)" in texto
    assert "VEREDICTO: aprovado" in r.reasons[-1]


def test_criteria_devolvido_e_auditavel(criterios) -> None:
    """GateResult guarda os limiares efetivamente usados."""
    r = gate_boltz(_hit_base(), criterios)
    assert r.criteria["ipsae_min"] == 0.15
    assert r.criteria["coverage_min"] == 0.5
    assert r.criteria["require_canonical_face"] is True


def test_metrica_secundaria_ausente_fica_registrada_sem_reprovar(criterios) -> None:
    """PAE/contatos ausentes (normal nestas planilhas) avisam, mas nao bloqueiam."""
    r = gate_boltz(_hit_base(), criterios)
    assert r.approved is True
    assert any("PAE de interface FALTANTE" in m for m in r.reasons)
    assert any("n_contacts FALTANTE" in m for m in r.reasons)


def test_strict_missing_transforma_ausencia_em_reprovacao(criterios) -> None:
    """Com strict_missing=true o mesmo HIT reprova por PAE/contatos ausentes."""
    duro = dict(criterios)
    duro["strict_missing"] = True
    r = gate_boltz(_hit_base(), duro)
    assert r.approved is False


def test_criterio_desconhecido_falha_alto() -> None:
    """Limiar com nome errado nao pode ser ignorado em silencio."""
    with pytest.raises(ValueError):
        gate_boltz(_hit_base(), {"ipsae_minimo": 0.15})


def test_scores_aceita_dict_alem_do_dataclass(criterios) -> None:
    """A ponte HTTP entrega dict; o gate tem de aceitar sem traducao manual."""
    r = gate_boltz(
        {
            "ipsae": 0.583,
            "iptm": 0.8096027970314026,
            "coverage_core_face": 1.0,
            "face": "nuclear (canonica)",
        },
        criterios,
    )
    assert r.approved is True


# --------------------------------------------------------------------------
# 4. GATE 2 (pos-HADDOCK3)
# --------------------------------------------------------------------------
def test_gate_haddock_tabela_vazia_reprova(criterios) -> None:
    """Sem cluster avaliado nao ha aprovacao por omissao."""
    r = gate_haddock([], criterios)
    assert r.approved is False
    assert any("VAZIO" in m for m in r.reasons)


def test_gate_haddock_aprova_por_dockq(criterios) -> None:
    """Melhor cluster por DockQ acima de 0.23 aprova."""
    r = gate_haddock(
        [
            {"cluster_rank": 1, "dockq": 0.11, "irmsd": 6.2},
            {"cluster_rank": 2, "dockq": 0.42, "irmsd": 3.1},
        ],
        criterios,
    )
    assert r.approved is True
    assert any("0.420" in m for m in r.reasons)


def test_gate_haddock_sem_metrica_capri_reprova(criterios) -> None:
    """Linhas sem DockQ nem iRMSD = caprieval sem referencia -> reprova."""
    r = gate_haddock([{"cluster_rank": 1, "score": -80.0}], criterios)
    assert r.approved is False
    assert any("NENHUM" in m for m in r.reasons)


def test_gate_haddock_celula_vazia_nao_vira_zero(criterios) -> None:
    """DockQ vazio nao pode ser lido como 0.0 (reprovaria por dado inventado)."""
    r = gate_haddock([{"cluster_rank": 1, "dockq": "", "irmsd": 2.0}], criterios)
    assert r.approved is True
    assert any("DockQ FALTANTE" in m for m in r.reasons)


# --------------------------------------------------------------------------
# limiares espelhados do motor (nao copiados a mao)
# --------------------------------------------------------------------------
def test_limiares_corroborantes_vem_do_ppi_config() -> None:
    """iface_iptm/PAE/contatos sao LIDOS de scoring_engine.config.SCORE, nao literais locais.

    Se o motor mudar um desses limiares, o guarda-chuva acompanha; divergir em
    silencio era o defeito que este teste trava.
    """
    ppi_config = pytest.importorskip("scoring_engine.config")
    assert DEFAULT_CRITERIA["iface_iptm_min"] == ppi_config.SCORE["iptm_maybe"]
    assert DEFAULT_CRITERIA["pae_iface_max"] == ppi_config.SCORE["pae_iface_max"]
    assert DEFAULT_CRITERIA["min_contacts"] == ppi_config.SCORE["min_contacts"]


def test_fallback_dos_limiares_espelhados_mantem_os_valores_de_hoje() -> None:
    """Sem o motor instalado, o funil usa exatamente os mesmos numeros."""
    from funnel.gate import _FALLBACK_ESPELHO

    assert _FALLBACK_ESPELHO == {
        "iface_iptm_min": 0.50,
        "pae_iface_max": 10.0,
        "min_contacts": 10,
    }
