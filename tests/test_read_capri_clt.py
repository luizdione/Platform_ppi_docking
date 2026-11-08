# -*- coding: utf-8 -*-
"""Leitura do `capri_clt.tsv` -- o elo que faltava entre o HADDOCK3 e o GATE 2.

O que estes testes travam:

* o bloco de 13 linhas ``#`` que o haddock3 escreve antes do cabecalho e
  descartado (com `csv.DictReader` direto o GATE 2 REPROVA um docking valido
  por "dado faltante" -- veredicto plausivel e ERRADO, o modo de falha que o
  projeto proibe);
* o placeholder ``"-"`` vira ``None``, nunca ``0.0`` (0.0 em `dockq` seria lido
  como "docking pessimo" em vez de "nao medido");
* arquivo ausente/so-comentario falha ALTO, em vez de devolver lista vazia.
"""

from __future__ import annotations

from pathlib import Path

import os

import pytest

from funnel.gate import gate_haddock, read_capri_clt

#: `capri_clt.tsv` REAL de um run do HADDOCK3, para o teste rodar contra a
#: saida de verdade quando ela existe. Vem de env, nunca de um literal de
#: disco: ou o caminho completo em ``PPI_DOCK_CAPRI_REAL``, ou o layout
#: padrao sob ``PPI_DOCK_DATA_ROOT``. Sem nenhuma das duas, o teste e pulado.
def _capri_real() -> "Path | None":
    direto = os.environ.get("PPI_DOCK_CAPRI_REAL", "").strip()
    if direto:
        return Path(direto)
    raiz = os.environ.get("PPI_DOCK_DATA_ROOT", "").strip()
    if not raiz:
        return None
    achados = sorted(Path(raiz).glob(
        "*/haddock/*/run-*/*_caprieval/capri_clt.tsv"
    ))
    return achados[0] if achados else None


CAPRI_REAL = _capri_real()

#: Cabecalho que o haddock3 escreve de verdade (13 linhas), reproduzido aqui
#: para o teste sinteticoveja o mesmo formato sem depender do E:.
_COMENTARIO = "\n".join(
    [
        "########################################",
        "# `caprieval` cluster-based analysis",
        "#",
        "# > sortby_key=score",
        "# > sort_ascending=True",
        "# > clt_threshold=4",
        "#",
        "# NOTE: if under_eval=yes, it means that there were less models in a cluster",
        "#    than clt_threshold, thus these values were under evaluated.",
        "#   You might need to tweak the value of clt_threshold or change some",
        "#    parameters in `clustfcc` depending on your analysis.",
        "#",
        "########################################",
    ]
)


@pytest.fixture()
def capri_sintetico(tmp_path) -> Path:
    """Um capri_clt.tsv no formato REAL: comentarios + cabecalho + 2 clusters."""
    cols = "cluster_rank\tcluster_id\tn\tunder_eval\tscore\tirmsd\tfnat\tlrmsd\tdockq"
    linhas = [
        _COMENTARIO,
        cols,
        "1\t1\t8\t-\t-12.5\t1.85\t0.72\t3.90\t0.68",
        "2\t2\t4\tyes\t-8.1\t6.40\t0.15\t14.20\t0.18",
    ]
    destino = tmp_path / "capri_clt.tsv"
    destino.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    return destino


def test_pula_o_bloco_de_comentario(capri_sintetico) -> None:
    """13 linhas '#' descartadas: sobram os 2 clusters, com as colunas certas."""
    linhas = read_capri_clt(capri_sintetico)
    assert len(linhas) == 2
    assert "dockq" in linhas[0]
    assert "irmsd" in linhas[0]
    # o bug classico: o DictReader ingenuo cria UMA coluna chamada '####...'
    assert not any(c.startswith("#") for c in linhas[0])


def test_converte_numeros_e_placeholder(capri_sintetico) -> None:
    """Numericos viram float; '-' vira None (jamais 0.0)."""
    linhas = read_capri_clt(capri_sintetico)
    assert linhas[0]["dockq"] == pytest.approx(0.68)
    assert linhas[0]["irmsd"] == pytest.approx(1.85)
    assert linhas[0]["under_eval"] is None  # era "-"
    assert linhas[1]["under_eval"] == "yes"  # texto preservado
    assert linhas[0]["dockq"] is not None and linhas[0]["dockq"] != 0.0


def test_gate2_aprova_a_tabela_valida(capri_sintetico) -> None:
    """REGRESSAO: a tabela lida corretamente APROVA (DockQ 0.68 >= 0.23).

    Com `csv.DictReader` direto no arquivo, este mesmo docking era REPROVADO
    com a mensagem "nenhum cluster traz DockQ ou iRMSD".
    """
    resultado = gate_haddock(read_capri_clt(capri_sintetico))
    assert resultado.approved is True
    assert any("DockQ" in m for m in resultado.reasons)


def test_dictreader_ingenuo_reprovaria(capri_sintetico) -> None:
    """Documenta o falso negativo que `read_capri_clt` existe para impedir."""
    import csv

    with capri_sintetico.open(encoding="utf-8") as fh:
        cruas = list(csv.DictReader(fh, delimiter="\t"))
    ruim = gate_haddock(cruas)
    assert ruim.approved is False  # mesmo docking, veredicto oposto


def test_arquivo_ausente_falha_alto(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        read_capri_clt(tmp_path / "nao_existe.tsv")


def test_so_comentario_falha_alto(tmp_path) -> None:
    """Run interrompido (so cabecalho) nao pode virar 'tabela vazia' silenciosa."""
    p = tmp_path / "capri_clt.tsv"
    p.write_text(_COMENTARIO + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="comentario"):
        read_capri_clt(p)


@pytest.mark.skipif(
    CAPRI_REAL is None or not CAPRI_REAL.is_file(),
    reason="nenhum capri_clt.tsv real apontado (PPI_DOCK_CAPRI_REAL / "
           "PPI_DOCK_DATA_ROOT)",
)
def test_arquivo_real_do_haddock() -> None:
    """Contra a saida REAL do haddock3: le a linha e o GATE 2 decide de verdade."""
    linhas = read_capri_clt(CAPRI_REAL)
    assert len(linhas) >= 1
    assert any(l.get("dockq") is not None for l in linhas)
    resultado = gate_haddock(linhas)
    # nao fixamos o veredicto (depende do run), mas ele NAO pode ser
    # "reprovado por dado faltante" -- isso seria o falso negativo de volta
    assert not any("dado FALTANTE" in m for m in resultado.reasons)
