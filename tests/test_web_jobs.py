# -*- coding: utf-8 -*-
"""Motor de jobs do painel: preflight, fila, limites por recurso e cancelamento.

Sem GPU, sem Docker e sem rodar nada pesado: os jobs destes testes sao
`python -c ...` de milissegundos. O que esta sob teste e a POLITICA -- quem
entra, quem espera, quem e recusado e o que acontece quando o servidor cai no
meio -- porque e exatamente ela que protege a maquina (GPU unica, RAM apertada).
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from web import jobs as motor


@pytest.fixture()
def motor_isolado(tmp_path, monkeypatch):
    """Motor com diretorio de jobs proprio e registro zerado."""
    monkeypatch.setenv("PPI_DOCK_JOBS_DIR", str(tmp_path))
    motor._JOBS.clear()
    yield motor
    for job in list(motor._JOBS.values()):
        if job.estado in motor.VIVOS and job.pid:
            try:
                import psutil

                psutil.Process(job.pid).kill()
            except Exception:  # noqa: BLE001
                pass
    motor._JOBS.clear()


def _esperar(job, alvos, limite_s=30.0):
    """Espera o job chegar num dos estados; devolve o estado final."""
    fim = time.time() + limite_s
    while time.time() < fim:
        motor._escalonar()
        if job.estado in alvos:
            return job.estado
        time.sleep(0.2)
    return job.estado


# --------------------------------------------------------------------------
# 1. preflight: a maquina aguenta?
# --------------------------------------------------------------------------
def test_preflight_leve_passa_sempre(motor_isolado, monkeypatch) -> None:
    """Job leve nao pode ser barrado por RAM: senao o painel para de funcionar."""
    monkeypatch.setattr(motor, "ram_livre", lambda: {"fracao": 0.01, "livre_gb": 0.3, "total_gb": 32})
    assert motor.preflight("leve")["ok"] is True


def test_preflight_recusa_pesado_com_ram_no_limite(motor_isolado, monkeypatch) -> None:
    """Abaixo de 20% de RAM livre, job pesado NAO entra (regra da casa)."""
    monkeypatch.setattr(
        motor, "ram_livre", lambda: {"fracao": 0.06, "livre_gb": 1.9, "total_gb": 31.9}
    )
    resultado = motor.preflight("cpu_heavy")
    assert resultado["ok"] is False
    # a mensagem tem de ser acionavel: numero de verdade, nao "recurso indisponivel"
    assert "94%" in resultado["motivo"] and "1.9 GB" in resultado["motivo"]


def test_preflight_recusa_gpu_ocupada(motor_isolado, monkeypatch) -> None:
    """Dois processos CUDA na mesma placa travam a maquina - nunca admitir o 2o."""
    monkeypatch.setattr(motor, "ram_livre", lambda: {"fracao": 0.9, "livre_gb": 28, "total_gb": 32})
    monkeypatch.setattr(
        motor, "gpu_ocupada", lambda: {"sabe": True, "ocupada": True, "processos": ["1234, 8000 MiB"]}
    )
    resultado = motor.preflight("gpu")
    assert resultado["ok"] is False
    assert "GPU" in resultado["motivo"]


def test_preflight_gpu_livre_passa(motor_isolado, monkeypatch) -> None:
    monkeypatch.setattr(motor, "ram_livre", lambda: {"fracao": 0.9, "livre_gb": 28, "total_gb": 32})
    monkeypatch.setattr(motor, "gpu_ocupada", lambda: {"sabe": True, "ocupada": False, "processos": []})
    assert motor.preflight("gpu")["ok"] is True


def test_preflight_sem_nvidia_smi_nao_inventa(motor_isolado, monkeypatch) -> None:
    """Sem leitura da GPU, o motor NAO afirma que esta ocupada (nao trava a fila)."""
    monkeypatch.setattr(motor, "ram_livre", lambda: {"fracao": 0.9, "livre_gb": 28, "total_gb": 32})
    monkeypatch.setattr(motor, "gpu_ocupada", lambda: {"sabe": False, "motivo": "nvidia-smi ausente"})
    assert motor.preflight("gpu")["ok"] is True


# --------------------------------------------------------------------------
# 2. ciclo de vida do job
# --------------------------------------------------------------------------
def test_job_roda_e_conclui(motor_isolado) -> None:
    job = motor.enfileirar(
        "eco", [sys.executable, "-c", "print('ola do job')"], classe="leve"
    )
    assert _esperar(job, {motor.CONCLUIDO, motor.ERRO}) == motor.CONCLUIDO
    assert job.rc == 0
    assert "ola do job" in job.log_path.read_text(encoding="utf-8")


def test_job_que_falha_vira_erro_com_o_log(motor_isolado) -> None:
    """Falha nao pode virar 'concluido': o estado vem do codigo de saida."""
    job = motor.enfileirar(
        "quebra",
        [sys.executable, "-c", "import sys; print('vou falhar'); sys.exit(3)"],
        classe="leve",
    )
    assert _esperar(job, {motor.CONCLUIDO, motor.ERRO}) == motor.ERRO
    assert job.rc == 3
    assert "vou falhar" in job.log_path.read_text(encoding="utf-8")


def test_classe_desconhecida_erra_alto(motor_isolado) -> None:
    with pytest.raises(ValueError):
        motor.enfileirar("x", [sys.executable, "-c", "pass"], classe="inventada")


# --------------------------------------------------------------------------
# 3. a trava que nao existia: limite por classe de recurso
# --------------------------------------------------------------------------
def test_segundo_job_pesado_espera_na_fila(motor_isolado, monkeypatch) -> None:
    """O ponto central da fase: dois jobs pesados NUNCA rodam juntos.

    Antes disso, dois POST em /haddock/run subiam dois containers ao mesmo tempo,
    disputando os 8 GB do WSL2.
    """
    monkeypatch.setattr(motor, "preflight", lambda classe: {"ok": True, "motivo": ""})
    primeiro = motor.enfileirar(
        "pesado 1", [sys.executable, "-c", "import time; time.sleep(6)"], classe="cpu_heavy"
    )
    segundo = motor.enfileirar(
        "pesado 2", [sys.executable, "-c", "print('nao devia rodar ainda')"], classe="cpu_heavy"
    )
    assert _esperar(primeiro, {motor.RODANDO}, 15) == motor.RODANDO
    motor._escalonar()
    assert segundo.estado == motor.NA_FILA, "o 2o pesado entrou junto - a trava falhou"

    # e o leve passa por fora, porque tem classe (e limite) proprios
    leve = motor.enfileirar("leve", [sys.executable, "-c", "pass"], classe="leve")
    assert _esperar(leve, {motor.CONCLUIDO}, 20) == motor.CONCLUIDO

    motor.cancelar(primeiro.id)


def test_job_pesado_fica_na_fila_quando_a_maquina_nao_aguenta(
    motor_isolado, monkeypatch
) -> None:
    """Recusa do preflight NAO vira erro: o job espera a maquina liberar."""
    monkeypatch.setattr(
        motor,
        "preflight",
        lambda classe: {"ok": False, "motivo": "RAM a 94% (1.9 GB livre)"},
    )
    job = motor.enfileirar("pesado", [sys.executable, "-c", "pass"], classe="gpu")
    motor._escalonar()
    assert job.estado == motor.NA_FILA
    assert "94%" in job.meta.get("espera", "")  # o motivo aparece na tela


# --------------------------------------------------------------------------
# 4. cancelamento e queda do servidor
# --------------------------------------------------------------------------
def test_cancelar_job_na_fila(motor_isolado, monkeypatch) -> None:
    monkeypatch.setattr(
        motor, "preflight", lambda classe: {"ok": False, "motivo": "segurando"}
    )
    job = motor.enfileirar("preso", [sys.executable, "-c", "pass"], classe="gpu")
    motor._escalonar()
    assert motor.cancelar(job.id)["ok"] is True
    assert job.estado == motor.CANCELADO


def test_cancelar_job_desconhecido_nao_estoura(motor_isolado) -> None:
    resultado = motor.cancelar("nao-existe")
    assert resultado["ok"] is False and "desconhecido" in resultado["erro"]


def test_job_orfao_de_restart_vira_desconhecido(motor_isolado, tmp_path) -> None:
    """Servidor caiu com job rodando: dizer 'rodando' seria mentira.

    O processo nao sobrevive ao servidor, entao ao recarregar o registro o job
    tem de virar `desconhecido` -- com o aviso de conferir o disco antes de
    repetir, ja que MD e HADDOCK sao resumiveis.
    """
    bruto = {
        "id": "abc123",
        "nome": "md antiga",
        "classe": "gpu",
        "cmd": [sys.executable, "-c", "pass"],
        "cwd": str(tmp_path),
        "estado": motor.RODANDO,
        "criado": time.time(),
        "inicio": time.time(),
        "fim": None,
        "pid": 999999,
        "rc": None,
        "erro": "",
        "meta": {},
    }
    (tmp_path / "abc123.json").write_text(json.dumps(bruto), encoding="utf-8")

    motor._JOBS.clear()
    motor._carregar_do_disco()

    recuperado = motor.obter("abc123")
    assert recuperado is not None
    assert recuperado.estado == motor.DESCONHECIDO
    assert "resumiveis" in recuperado.erro


def test_registro_sobrevive_ao_restart(motor_isolado) -> None:
    """O historico nao pode evaporar quando o servico reinicia (antes evaporava)."""
    job = motor.enfileirar("eco", [sys.executable, "-c", "pass"], classe="leve")
    _esperar(job, {motor.CONCLUIDO, motor.ERRO})
    motor._JOBS.clear()
    motor._carregar_do_disco()
    assert motor.obter(job.id) is not None


# --------------------------------------------------------------------------
# 5. foto dos recursos (a tela le isto)
# --------------------------------------------------------------------------
def test_recursos_relata_ocupacao_por_classe(motor_isolado) -> None:
    foto = motor.recursos()
    assert set(foto["classes"]) == set(motor.LIMITES)
    assert foto["classes"]["gpu"]["limite"] == 1
    assert foto["classes"]["cpu_heavy"]["limite"] == 1
    assert "ram" in foto and "gpu" in foto

# --------------------------------------------------------------------------
# 6. esteira: uma etapa so comeca depois que a anterior CONCLUIR
# --------------------------------------------------------------------------
def test_esteira_segura_a_proxima_ate_a_anterior_concluir(motor_isolado) -> None:
    """O encadeamento e o que torna a cascata (Boltz->funil->HADDOCK->MD) segura."""
    primeiro = motor.enfileirar(
        "etapa 1", [sys.executable, "-c", "import time; time.sleep(4)"], classe="leve"
    )
    segundo = motor.enfileirar(
        "etapa 2", [sys.executable, "-c", "print('so depois')"],
        classe="leve", depende_de=primeiro.id,
    )
    assert _esperar(primeiro, {motor.RODANDO}, 15) == motor.RODANDO
    motor._escalonar()
    assert segundo.estado == motor.NA_FILA, "a 2a etapa entrou antes da 1a terminar"
    assert "etapa anterior" in segundo.meta.get("espera", "")

    assert _esperar(primeiro, {motor.CONCLUIDO}, 30) == motor.CONCLUIDO
    assert _esperar(segundo, {motor.CONCLUIDO, motor.ERRO}, 30) == motor.CONCLUIDO


def test_esteira_cancela_as_seguintes_quando_uma_falha(motor_isolado) -> None:
    """Rodar MD sobre um HADDOCK que quebrou so produz numero com cara de dado."""
    primeiro = motor.enfileirar(
        "etapa que falha", [sys.executable, "-c", "import sys; sys.exit(2)"], classe="leve"
    )
    segundo = motor.enfileirar(
        "etapa seguinte", [sys.executable, "-c", "print('NAO devia rodar')"],
        classe="leve", depende_de=primeiro.id,
    )
    assert _esperar(primeiro, {motor.ERRO}, 30) == motor.ERRO
    assert _esperar(segundo, {motor.CANCELADO, motor.CONCLUIDO}, 30) == motor.CANCELADO
    assert "nao concluiu" in segundo.erro
    assert not segundo.log_path.exists() or "NAO devia rodar" not in segundo.log_path.read_text(
        encoding="utf-8"
    )


def test_dependencia_desconhecida_erra_alto(motor_isolado) -> None:
    with pytest.raises(ValueError):
        motor.enfileirar(
            "orfa", [sys.executable, "-c", "pass"], classe="leve", depende_de="nao-existe"
        )


# --------------------------------------------------------------------------
# 7. comandos das etapas (a etapa avulsa e a da esteira sao o MESMO comando)
# --------------------------------------------------------------------------
def test_etapas_da_cascata_tem_classe_de_recurso_correta() -> None:
    """Classe errada aqui = job pesado escapando da trava. Vale travar por teste."""
    from web.api.jobs import ETAPAS_SEQUENCIA

    por_id = {e["id"]: e for e in ETAPAS_SEQUENCIA}
    assert [e["id"] for e in ETAPAS_SEQUENCIA] == [
        "boltz", "funil", "haddock", "md", "energia"
    ]
    assert por_id["boltz"]["classe"] == "gpu"
    assert por_id["funil"]["classe"] == "leve"
    assert por_id["haddock"]["classe"] == "cpu_heavy"
    assert por_id["md"]["classe"] == "gpu"
    for e in ETAPAS_SEQUENCIA:
        assert e["classe"] in motor.LIMITES


def test_comando_da_md_retoma_por_padrao() -> None:
    """MD dura DIAS e e resumivel: `--fresh` so entra se pedido explicitamente."""
    from web.api.jobs import _cmd_etapa

    cmd = _cmd_etapa("md", "DEMO01", {"run": "r", "md_dir": "d"})
    assert "--fresh" not in cmd
    assert cmd[-4:] == ["--tier", "fast", "--ff", "charmm36m"]

    com_fresh = _cmd_etapa("md", "DEMO01", {"run": "r", "md_dir": "d", "fresh": True})
    assert "--fresh" in com_fresh


def test_comando_sem_parametro_obrigatorio_erra_alto() -> None:
    """Falta de caminho tem de virar 422 legivel, nao um job que falha depois."""
    from fastapi import HTTPException

    from web.api.jobs import _cmd_etapa

    with pytest.raises(HTTPException) as exc:
        _cmd_etapa("md", "DEMO01", {"run": "so-isso"})
    assert "md_dir" in str(exc.value.detail)

    with pytest.raises(HTTPException):
        _cmd_etapa("boltz", "DEMO01", {})


def test_sequencia_respeita_a_ordem_da_cascata() -> None:
    """Mesmo pedida fora de ordem, a esteira sai na ordem cientifica."""
    from web.api.jobs import ETAPAS_SEQUENCIA

    ordem = [e["id"] for e in ETAPAS_SEQUENCIA]
    pedido = ["md", "funil", "haddock"]
    esperado = [e for e in ordem if e in pedido]
    assert esperado == ["funil", "haddock", "md"]


# ---------------------------------------------------------------------------
# GPU ocupada: o criterio e a memoria de video, nao a lista de processos
# ---------------------------------------------------------------------------
def _nvidia_falso(gpu: str, apps: str):
    """Substitui a consulta ao nvidia-smi por saidas fixas."""
    def _resposta(_exe, consulta):
        return gpu if consulta.startswith("--query-gpu") else apps
    return _resposta


def _ram_com_folga(monkeypatch) -> None:
    """Fixa a RAM com folga: estes testes avaliam a GPU, nao a memoria.

    `preflight` confere a RAM antes da placa, entao sem isto o veredicto
    dependeria de quanta memoria o host tem livre no momento do teste.
    """
    monkeypatch.setattr(
        motor,
        "ram_livre",
        lambda: {"fracao": 0.80, "livre_gb": 51.2, "total_gb": 64.0},
    )


def test_gpu_livre_apesar_da_lista_de_processos_do_windows(monkeypatch) -> None:
    """Cliente grafico do Windows nao pode bloquear job de GPU.

    No Windows (WDDM) o `--query-compute-apps` lista explorer.exe, menu iniciar
    e WebView -- todos com `[N/A]` de memoria. O criterio antigo era "tem linha
    na lista = ocupada", o que tornava TODO job de GPU impossivel nesta maquina:
    o proprio desktop segurava a placa para sempre.
    """
    _ram_com_folga(monkeypatch)
    monkeypatch.setattr(motor.shutil, "which", lambda _: r"C:\fake\nvidia-smi.exe")
    monkeypatch.setattr(
        motor,
        "_nvidia",
        _nvidia_falso(
            "1339, 12288, 3\n",
            r"11232, [N/A], C:\Windows\explorer.exe" "\n"
            "13552, [N/A], StartMenuExperienceHost.exe\n",
        ),
    )
    estado = motor.gpu_ocupada()
    assert estado["sabe"] is True
    assert estado["ocupada"] is False, "desktop do Windows nao e trabalho de GPU"
    assert estado["memoria_mib"] == 1339
    assert motor.preflight("gpu")["ok"] is True


def test_gpu_ocupada_quando_a_memoria_de_video_esta_tomada(monkeypatch) -> None:
    """MD/Boltz em curso (varios GB) tem de barrar o segundo job CUDA."""
    _ram_com_folga(monkeypatch)
    monkeypatch.setattr(motor.shutil, "which", lambda _: r"C:\fake\nvidia-smi.exe")
    monkeypatch.setattr(
        motor, "_nvidia", _nvidia_falso("7420, 12288, 97\n", "")
    )
    estado = motor.gpu_ocupada()
    assert estado["ocupada"] is True
    veredicto = motor.preflight("gpu")
    assert veredicto["ok"] is False
    assert "7420" in veredicto["motivo"], "a mensagem tem de trazer o numero medido"


def test_sem_nvidia_smi_nao_afirma_nada(monkeypatch) -> None:
    """Sem a ferramenta, o preflight nao pode inventar que a GPU esta livre nem presa."""
    _ram_com_folga(monkeypatch)
    monkeypatch.setattr(motor.shutil, "which", lambda _: None)
    estado = motor.gpu_ocupada()
    assert estado["sabe"] is False
    # nao sabendo, o job PASSA (a alternativa seria travar a plataforma inteira
    # numa maquina sem nvidia-smi) -- mas sem afirmar que a placa esta livre
    assert "ocupada" not in estado
    assert motor.preflight("gpu")["ok"] is True
