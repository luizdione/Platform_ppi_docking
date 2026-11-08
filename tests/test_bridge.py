# -*- coding: utf-8 -*-
"""Testes da ponte HTTP (bridge) - sem rede, sem GPU, sem haddock3 instalado.

Cobrem o que a ponte PROMETE ao n8n:

* toda resposta de erro tem corpo ``{"error": ...}`` (nunca 200 com resultado
  plausivel);
* operacao do funil ainda inexistente vira 501, e dado ausente vira 422 -
  nunca 500;
* job longo que quebra termina em status "error", nunca "done";
* ``run_cmd`` executa por argv (sem shell) e reporta returncode/timeout.
"""

from __future__ import annotations

import sys
import time

import pytest

from fastapi.testclient import TestClient

from bridge import runners
from bridge.app import app

MANIFESTO_MINIMO = {
    "campaign": "DEMO01_barnase_barstar",
    "pair_id": "barnase_x_barstarD39A",
    "bait": "barnase",
    "partner": "barstarD39A",
}


@pytest.fixture()
def cliente() -> TestClient:
    """Cliente HTTP em processo (erros do servidor viram resposta, nao excecao)."""
    return TestClient(app, raise_server_exceptions=False)


def test_health_reporta_toolchain(cliente: TestClient) -> None:
    """/health responde 200 e diz se o binario haddock3 existe (hoje: nao)."""
    r = cliente.get("/health")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["ok"] is True
    assert isinstance(corpo["toolchain"]["haddock3"], bool)


def test_gate_aprova_hit_real_do_demo01(cliente: TestClient) -> None:
    """Scores do hit barstarD39A (ipSAE 0.583) passam no GATE 1."""
    r = cliente.post(
        "/gate",
        json={
            "scores": {
                "ipsae": 0.583,
                "coverage_core_face": 1.0,
                "face": "nuclear (canonica)",
            }
        },
    )
    assert r.status_code == 200
    assert r.json()["approved"] is True


def test_gate_recusa_scores_nao_numericos(cliente: TestClient) -> None:
    """Celula de planilha com lixo vira 422 explicito - nunca 0.0 silencioso."""
    r = cliente.post("/gate", json={"scores": {"ipsae": "abc"}})
    assert r.status_code == 422
    assert "error" in r.json()


def test_score_responde_coerente_sem_o_dado_em_disco(cliente: TestClient) -> None:
    """/score nunca pode responder 500 por dado ausente.

    Tres desfechos legitimos, e nenhum deles e 500:

    * **501** - o modulo de scoring do funil nao existe (diz o que faltou);
    * **422** - a campanha esta declarada mas os DADOS dela nao estao nesta
      maquina (a campanha de exemplo aponta para uma raiz que o usuario
      monta; a mensagem traz o caminho que faltou);
    * **200** - ha dado em disco e os pares vem na resposta.
    """
    r = cliente.post("/score", json={"subproject": "DEMO01_barnase_barstar"})
    assert r.status_code in (200, 422, 501), r.text
    if r.status_code == 501:
        assert "funnel" in r.json()["error"]
    elif r.status_code == 422:
        assert r.json()["error"], "422 sem explicar o que faltou"
    else:
        assert "pairs" in r.json()


def test_prepare_sem_workdir_vira_422(cliente: TestClient, monkeypatch) -> None:
    """A ponte nao escolhe destino no disco por conta propria."""
    monkeypatch.delenv("PPI_DOCK_WORKDIR", raising=False)
    r = cliente.post("/haddock/prepare", json={"manifest": MANIFESTO_MINIMO})
    assert r.status_code == 422
    assert "workdir" in r.json()["error"]


def test_prepare_com_manifesto_incompleto_vira_422(cliente: TestClient, tmp_path) -> None:
    """Sem residuos ativos nao ha AIR: erro do pedido (422), nao 500 generico."""
    r = cliente.post(
        "/haddock/prepare",
        json={"manifest": MANIFESTO_MINIMO, "workdir": str(tmp_path)},
    )
    assert r.status_code == 422
    assert "error" in r.json()


def test_manifesto_invalido_vira_422(cliente: TestClient) -> None:
    """Identidade incompleta (sem pair_id) e recusada antes de qualquer trabalho."""
    r = cliente.post("/haddock/prepare", json={"manifest": {"campaign": "x"}})
    assert r.status_code == 422


def test_status_de_job_desconhecido_vira_404(cliente: TestClient) -> None:
    """Polling de job inexistente nao pode responder 200."""
    r = cliente.get("/status/naoexiste")
    assert r.status_code == 404
    assert "error" in r.json()


def test_haddock_run_falha_no_job_e_nao_no_200(cliente: TestClient, tmp_path) -> None:
    """/haddock/run devolve job_id; o job termina em "error" (sem AIR/binario)."""
    r = cliente.post(
        "/haddock/run",
        json={"manifest": MANIFESTO_MINIMO, "workdir": str(tmp_path)},
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    for _ in range(50):
        estado = cliente.get(f"/status/{job_id}").json()
        if estado["status"] != "running":
            break
        time.sleep(0.1)
    assert estado["status"] == "error"
    assert estado["result"] is None
    assert estado["error"]


def test_run_cmd_sem_shell(tmp_path) -> None:
    """run_cmd roda por argv e devolve returncode/stdout reais."""
    saida = runners.run_cmd([sys.executable, "-c", "print(41+1)"], cwd=tmp_path)
    assert saida["returncode"] == 0
    assert saida["stdout"].strip() == "42"
    assert saida["timeout"] is False


def test_run_cmd_timeout_nao_finge_sucesso() -> None:
    """Timeout vira returncode None + timeout=True, nunca 0."""
    saida = runners.run_cmd(
        [sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5
    )
    assert saida["timeout"] is True
    assert saida["returncode"] is None


def test_job_que_quebra_fica_em_error() -> None:
    """Excecao dentro do job nao pode virar 'done'."""

    def _quebra(log):
        log("vou quebrar")
        raise RuntimeError("falha proposital")

    job = runners.start_job(_quebra, name="teste-erro")
    for _ in range(50):
        atual = runners.get_job(job.job_id)
        if atual.status != "running":
            break
        time.sleep(0.1)
    assert atual.status == "error"
    assert "falha proposital" in atual.error
    assert atual.result is None


def test_job_ok_guarda_resultado() -> None:
    """Job bem-sucedido guarda o retorno JSON-safe e escreve log em disco."""
    from pathlib import Path

    def _ok(log):
        log("trabalhando")
        return {"ok": True}

    job = runners.start_job(_ok, name="teste-ok")
    for _ in range(50):
        atual = runners.get_job(job.job_id)
        if atual.status != "running":
            break
        time.sleep(0.1)
    assert atual.status == "done"
    assert atual.result == {"ok": True}
    assert Path(atual.log_path).is_file()


# --------------------------------------------------------------------------
# papeis de cadeia declarados chegam ao funil pela ponte
# --------------------------------------------------------------------------
def test_chain_roles_da_campanha_sao_carregados_do_yaml() -> None:
    """`subproject` no pedido = bloco chain_roles do YAML, nao papel posicional."""
    from bridge.app import _chain_roles

    bloco = _chain_roles("DEMO01")
    # os papeis vem DECLARADOS no YAML, nao da posicao das cadeias no .cif
    assert bloco["receptor"]["chain"] == "A"
    assert bloco["ligand"]["chain"] == "B"
    assert bloco["fail_if_ambiguous"] is True
    assert _chain_roles(None) is None          # sem campanha, sem declaracao


def test_campanha_desconhecida_vira_422(cliente: TestClient, tmp_path) -> None:
    """Campanha inexistente nao pode preparar o par com o papel chutado."""
    r = cliente.post(
        "/haddock/prepare",
        json={
            "manifest": MANIFESTO_MINIMO,
            "workdir": str(tmp_path),
            "subproject": "SUB99_inexistente",
        },
    )
    assert r.status_code == 422
    assert "SUB99_inexistente" in r.json()["error"]


def test_implementacao_sem_chain_roles_vira_501() -> None:
    """Funil que ignora os papeis declarados = 501 explicito, nunca preparo mudo."""
    from fastapi import HTTPException

    from bridge.app import _chamar_com_papeis

    def _sem_papeis(manifesto, workdir):
        return "nao deveria ter sido chamado"

    assert _chamar_com_papeis(_sem_papeis, 1, 2, chain_roles=None) == (
        "nao deveria ter sido chamado"
    )
    with pytest.raises(HTTPException) as exc:
        _chamar_com_papeis(_sem_papeis, 1, 2, chain_roles={"receptor": {"chain": "B"}})
    assert exc.value.status_code == 501
