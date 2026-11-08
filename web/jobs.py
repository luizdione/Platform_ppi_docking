# -*- coding: utf-8 -*-
"""Motor de jobs do painel: fila, travas por recurso e estado que sobrevive.

Este e o pedaco que faltava para o painel EXECUTAR, e nao so ler. Ele resolve
tres problemas concretos desta maquina:

1. **Nada de dois jobs pesados juntos.** A placa e uma so: dois
   processos CUDA batem no teto de VRAM e congelam a tela (TDR ja documentado).
   O HADDOCK3 roda no WSL2, que divide 8 GB com o Docker. Entao cada job declara
   uma CLASSE de recurso e a fila respeita um limite por classe -- antes NAO
   havia trava nenhuma: dois POST em /haddock/run subiam dois containers.
2. **Nao entrar em job pesado com a maquina no limite.** Antes de admitir um job
   de `gpu`/`cpu_heavy` o motor confere RAM livre (regra do autor: >=20%) e, para
   GPU, se ja ha outro processo CUDA. Recusa com motivo legivel em vez de
   empurrar a maquina para swap.
3. **Estado que nao evapora.** O registro antigo vivia so em memoria: reiniciar
   o servico apagava os jobs. Aqui cada job e um JSON em disco, com o log ao
   lado. Reiniciou? Os jobs continuam la; os que estavam rodando viram
   `desconhecido` (com o PID registrado) em vez de mentir "rodando".

O trabalho em si roda SEMPRE como subprocesso (argv), nunca em thread: alem de
nao segurar o GIL do servidor, da cancelamento de verdade (matar o processo) e
isolamento de falha.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List

# --- estados -------------------------------------------------------------
NA_FILA = "na_fila"
RODANDO = "rodando"
CONCLUIDO = "concluido"
ERRO = "erro"
CANCELADO = "cancelado"
DESCONHECIDO = "desconhecido"  # ficou rodando quando o servidor caiu

VIVOS = (NA_FILA, RODANDO)

# --- classes de recurso --------------------------------------------------
#: Quantos jobs de cada classe podem rodar ao mesmo tempo. `gpu` e `cpu_heavy`
#: sao 1 de proposito: a GPU e unica e o WSL2 tem 8 GB.
LIMITES: "Dict[str, int]" = {"gpu": 1, "cpu_heavy": 1, "leve": 4}

#: Fracao MINIMA de RAM livre para admitir job pesado (regra do autor: 20%).
RAM_LIVRE_MINIMA = 0.20


def jobs_dir() -> Path:
    """Onde vivem os JSONs e os logs (mesma env da ponte antiga)."""
    raiz = os.environ.get("PPI_DOCK_JOBS_DIR")
    destino = Path(raiz) if raiz else Path.home() / ".ppi_docking_jobs"
    destino.mkdir(parents=True, exist_ok=True)
    return destino


# ---------------------------------------------------------------------------
# preflight: a maquina aguenta?
# ---------------------------------------------------------------------------
def ram_livre() -> "Dict[str, Any]":
    """RAM livre agora. `fracao` in [0,1]; `None` se nao der para medir."""
    try:
        import psutil

        mem = psutil.virtual_memory()
        return {
            "fracao": mem.available / mem.total,
            "livre_gb": round(mem.available / 1024**3, 1),
            "total_gb": round(mem.total / 1024**3, 1),
        }
    except Exception as exc:  # noqa: BLE001
        return {"fracao": None, "erro": f"{type(exc).__name__}: {exc}"}


#: Acima de quanta memoria de video a placa conta como OCUPADA por trabalho de
#: verdade. O numero existe porque no Windows (WDDM) o proprio desktop
#: (explorer, menu iniciar, WebView) aparece no `--query-compute-apps` e
#: segura mais de 1 GB de VRAM: o criterio "tem processo na lista = ocupada"
#: tornaria todo job de GPU impossivel. Predicao de complexo e dinamica
#: molecular passam folgado de 4 GB, entao 2,5 GB separa os dois casos com
#: margem dos dois lados. Ajuste para a sua placa se precisar.
GPU_MIB_OCUPADA = 2500


def _nvidia(exe: str, consulta: str) -> "str | None":
    """Roda uma consulta do nvidia-smi e devolve a saida crua (None se falhar)."""
    try:
        proc = subprocess.run(
            [exe, consulta, "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:  # noqa: BLE001 - diagnostico nunca pode explodir
        return None
    return proc.stdout if proc.returncode == 0 else None


def gpu_ocupada() -> "Dict[str, Any]":
    """Ha trabalho de verdade na GPU? Usa `nvidia-smi`; sem ele, nao afirma nada.

    O criterio e a MEMORIA DE VIDEO OCUPADA, nao a lista de processos. No
    Windows (WDDM) `--query-compute-apps` lista tambem clientes graficos --
    explorer.exe, menu iniciar, WebView -- todos com `used_memory = [N/A]`; pela
    lista, a placa estaria "ocupada" 24h por dia e nenhum job de GPU jamais seria
    admitido. Pior: para um processo CUDA de verdade o Windows tambem costuma
    reportar `[N/A]`, entao nem filtrar o `[N/A]` resolve -- a lista simplesmente
    nao serve de medida aqui. A memoria total ocupada serve, e e a mesma leitura
    que a gente faria olhando o `nvidia-smi` na mao.
    """
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"sabe": False, "motivo": "nvidia-smi ausente"}

    bruto = _nvidia(exe, "--query-gpu=memory.used,memory.total,utilization.gpu")
    if not bruto or not bruto.strip():
        return {"sabe": False, "motivo": "nvidia-smi nao respondeu a consulta da GPU"}
    try:
        usada, total, uso = (int(float(x)) for x in bruto.splitlines()[0].split(","))
    except (ValueError, IndexError):
        return {"sabe": False, "motivo": f"saida inesperada do nvidia-smi: {bruto[:120]!r}"}

    apps = _nvidia(exe, "--query-compute-apps=pid,used_memory,process_name") or ""
    processos = [l.strip() for l in apps.splitlines() if l.strip()]
    return {
        "sabe": True,
        "ocupada": usada >= GPU_MIB_OCUPADA,
        "memoria_mib": usada,
        "total_mib": total,
        "uso_pct": uso,
        "limiar_mib": GPU_MIB_OCUPADA,
        # so informativo: no Windows esta lista inclui cliente grafico
        "processos": processos,
    }


def preflight(classe: str) -> "Dict[str, Any]":
    """Diz se da para admitir um job desta classe AGORA, e por que nao.

    Job leve passa sempre. Pesado confere RAM; de GPU confere tambem se a placa
    ja esta ocupada. A mensagem e escrita para ser lida na tela, com numero --
    "RAM a 94% (1,9 GB livre)" ajuda muito mais que "recurso indisponivel".
    """
    if classe == "leve":
        return {"ok": True, "motivo": ""}

    ram = ram_livre()
    fracao = ram.get("fracao")
    if fracao is not None and fracao < RAM_LIVRE_MINIMA:
        usada = round((1 - fracao) * 100)
        return {
            "ok": False,
            "motivo": (
                f"RAM a {usada}% ({ram.get('livre_gb')} GB livre de "
                f"{ram.get('total_gb')} GB). O limite para job pesado e "
                f"{round(RAM_LIVRE_MINIMA * 100)}% livre - feche o que puder."
            ),
            "ram": ram,
        }

    if classe == "gpu":
        gpu = gpu_ocupada()
        if gpu.get("sabe") and gpu.get("ocupada"):
            return {
                "ok": False,
                "motivo": (
                    f"a GPU ja esta com {gpu.get('memoria_mib')} MiB de "
                    f"{gpu.get('total_mib')} MiB ocupados (limite para admitir "
                    f"job novo: {gpu.get('limiar_mib')} MiB) e {gpu.get('uso_pct')}% "
                    "de uso. A placa e unica: dois jobs CUDA juntos travam a "
                    "maquina. Veja em Jobs se ha MD ou Boltz em curso."
                ),
                "gpu": gpu,
            }
    return {"ok": True, "motivo": "", "ram": ram}


# ---------------------------------------------------------------------------
# o job
# ---------------------------------------------------------------------------
@dataclass
class Job:
    id: str
    nome: str
    classe: str
    cmd: "List[str]"
    cwd: str
    estado: str = NA_FILA
    criado: float = 0.0
    inicio: "float | None" = None
    fim: "float | None" = None
    pid: "int | None" = None
    rc: "int | None" = None
    erro: str = ""
    #: id do job que precisa CONCLUIR antes deste comecar. E o que transforma
    #: jobs soltos numa esteira (Boltz -> funil -> HADDOCK -> MD -> energia):
    #: se a etapa anterior falhar, esta e cancelada em vez de rodar sobre lixo.
    depende_de: "str | None" = None
    #: diretorio onde este job grava JSON e log, FIXADO no nascimento. Resolver
    #: `jobs_dir()` a cada escrita fazia o job migrar de pasta se a env mudasse
    #: no meio do caminho -- foi assim que jobs de teste vazaram para o
    #: diretorio real do usuario (a thread do job gravava depois que o teste ja
    #: tinha restaurado a env).
    dir: str = ""
    meta: "Dict[str, Any]" = field(default_factory=dict)

    @property
    def _raiz(self) -> Path:
        return Path(self.dir) if self.dir else jobs_dir()

    @property
    def log_path(self) -> Path:
        return self._raiz / f"{self.id}.log"

    @property
    def json_path(self) -> Path:
        return self._raiz / f"{self.id}.json"

    def duracao_s(self) -> "float | None":
        if self.inicio is None:
            return None
        return round((self.fim or time.time()) - self.inicio, 1)

    def to_dict(self) -> "Dict[str, Any]":
        d = asdict(self)
        d["duracao_s"] = self.duracao_s()
        d["log"] = str(self.log_path)
        return d

    def gravar(self) -> None:
        try:
            self.json_path.write_text(
                json.dumps(asdict(self), ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # perder o registro nao pode derrubar o job


# ---------------------------------------------------------------------------
# registro + fila
# ---------------------------------------------------------------------------
_JOBS: "Dict[str, Job]" = {}
_LOCK = threading.RLock()
_ACORDA = threading.Event()
_ESCALONADOR: "threading.Thread | None" = None


def _carregar_do_disco() -> None:
    """Le os jobs gravados e conserta os que ficaram 'rodando' de um restart."""
    for arquivo in sorted(jobs_dir().glob("*.json")):
        try:
            bruto = json.loads(arquivo.read_text(encoding="utf-8"))
            job = Job(**bruto)
        except (OSError, ValueError, TypeError):
            continue
        job.dir = job.dir or str(arquivo.parent)
        if job.estado in VIVOS:
            # o processo nao sobrevive ao servidor: dizer "rodando" seria mentira
            job.estado = DESCONHECIDO
            job.erro = (
                "o servidor reiniciou enquanto este job estava em curso; "
                "confira o log e o resultado em disco antes de repetir "
                "(MD e HADDOCK sao resumiveis)"
            )
            job.gravar()
        _JOBS[job.id] = job


def _vagas(classe: str) -> int:
    limite = LIMITES.get(classe, 1)
    rodando = sum(1 for j in _JOBS.values() if j.estado == RODANDO and j.classe == classe)
    return limite - rodando


def _iniciar(job: Job) -> None:
    """Sobe o subprocesso do job e acompanha ate o fim (uma thread por job)."""
    job.estado = RODANDO
    job.inicio = time.time()
    job.gravar()

    def _acompanhar() -> None:
        try:
            with open(job.log_path, "a", encoding="utf-8", errors="replace") as log:
                log.write(
                    f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} inicio: "
                    f"{' '.join(job.cmd)}\n"
                )
                log.flush()
                proc = subprocess.Popen(
                    job.cmd,
                    cwd=job.cwd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                with _LOCK:
                    job.pid = proc.pid
                    job.gravar()
                rc = proc.wait()
                log.write(
                    f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} fim (rc={rc})\n"
                )
            with _LOCK:
                job.rc = rc
                job.fim = time.time()
                if job.estado == CANCELADO:
                    pass  # cancelamento ja registrou o motivo
                elif rc == 0:
                    job.estado = CONCLUIDO
                else:
                    job.estado = ERRO
                    job.erro = job.erro or f"processo terminou com rc={rc}"
                job.gravar()
        except Exception as exc:  # noqa: BLE001
            with _LOCK:
                job.estado = ERRO
                job.erro = f"{type(exc).__name__}: {exc}"
                job.fim = time.time()
                job.gravar()
        finally:
            _ACORDA.set()  # liberou vaga: reavalia a fila

    threading.Thread(target=_acompanhar, name=f"job-{job.id}", daemon=True).start()


def _escalonar() -> None:
    """Admite jobs da fila enquanto houver vaga e a maquina aguentar."""
    with _LOCK:
        na_fila = [j for j in _JOBS.values() if j.estado == NA_FILA]
        na_fila.sort(key=lambda j: j.criado)
        for job in na_fila:
            # --- esteira: so entra depois que a etapa anterior concluir -----
            if job.depende_de:
                anterior = _JOBS.get(job.depende_de)
                if anterior is None:
                    job.estado = CANCELADO
                    job.erro = (
                        f"a etapa anterior ({job.depende_de}) sumiu do registro"
                    )
                    job.fim = time.time()
                    job.gravar()
                    continue
                if anterior.estado in VIVOS:
                    job.meta["espera"] = f"aguardando a etapa anterior: {anterior.nome}"
                    continue
                if anterior.estado != CONCLUIDO:
                    # rodar a etapa seguinte sobre uma etapa que falhou so
                    # produziria lixo com aparencia de resultado
                    job.estado = CANCELADO
                    job.erro = (
                        f"etapa anterior nao concluiu ({anterior.estado}): "
                        f"{anterior.nome}"
                    )
                    job.fim = time.time()
                    job.gravar()
                    continue
                job.meta.pop("espera", None)

            if _vagas(job.classe) <= 0:
                continue
            checagem = preflight(job.classe)
            if not checagem["ok"]:
                # nao vira erro: continua na fila e tenta de novo no proximo ciclo
                job.meta["espera"] = checagem["motivo"]
                job.gravar()
                continue
            job.meta.pop("espera", None)
            _iniciar(job)


def _laco_escalonador() -> None:
    while True:
        try:
            _escalonar()
        except Exception:  # noqa: BLE001 - o escalonador nunca pode morrer
            pass
        # acorda por evento (job terminou / job novo) ou a cada 15 s, para
        # reavaliar o preflight de quem esta esperando RAM/GPU liberar
        _ACORDA.wait(timeout=15.0)
        _ACORDA.clear()


def iniciar_motor() -> None:
    """Carrega o historico e sobe o escalonador (idempotente)."""
    global _ESCALONADOR
    with _LOCK:
        if not _JOBS:
            _carregar_do_disco()
        if _ESCALONADOR is None or not _ESCALONADOR.is_alive():
            _ESCALONADOR = threading.Thread(
                target=_laco_escalonador, name="escalonador", daemon=True
            )
            _ESCALONADOR.start()


# ---------------------------------------------------------------------------
# API do motor
# ---------------------------------------------------------------------------
def enfileirar(
    nome: str,
    cmd: "List[str]",
    *,
    classe: str = "leve",
    cwd: "str | None" = None,
    meta: "Dict[str, Any] | None" = None,
    depende_de: "str | None" = None,
) -> Job:
    """Poe um job na fila. Ele so comeca quando houver vaga E a maquina aguentar.

    Com `depende_de`, so comeca depois que aquele job CONCLUIR -- e e cancelado
    se a etapa anterior falhar. E assim que a esteira (Boltz -> funil ->
    HADDOCK -> MD -> energia) e montada: uma corrente de jobs, cada um com a sua
    classe de recurso, sem ninguem segurando o servidor esperando.
    """
    if classe not in LIMITES:
        raise ValueError(f"classe de recurso desconhecida: {classe!r}")
    if depende_de and depende_de not in _JOBS:
        raise ValueError(f"job anterior desconhecido: {depende_de!r}")
    job = Job(
        id=uuid.uuid4().hex[:12],
        nome=nome,
        classe=classe,
        cmd=list(cmd),
        cwd=cwd or str(Path(__file__).resolve().parent.parent),
        criado=time.time(),
        depende_de=depende_de,
        dir=str(jobs_dir()),
        meta=dict(meta or {}),
    )
    with _LOCK:
        _JOBS[job.id] = job
        job.gravar()
    _ACORDA.set()
    return job


def obter(job_id: str) -> "Job | None":
    return _JOBS.get(job_id)


def listar(limite: int = 50) -> "List[Dict[str, Any]]":
    with _LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j.criado, reverse=True)
    return [j.to_dict() for j in jobs[:limite]]


def cancelar(job_id: str) -> "Dict[str, Any]":
    """Cancela um job na fila ou mata o processo em curso."""
    job = _JOBS.get(job_id)
    if job is None:
        return {"ok": False, "erro": f"job desconhecido: {job_id!r}"}
    with _LOCK:
        if job.estado == NA_FILA:
            job.estado = CANCELADO
            job.erro = "cancelado antes de comecar"
            job.fim = time.time()
            job.gravar()
            return {"ok": True, "estado": job.estado}
        if job.estado != RODANDO:
            return {"ok": False, "erro": f"job ja esta em {job.estado!r}"}
        job.estado = CANCELADO
        job.erro = "cancelado pelo usuario"
        job.gravar()
    try:
        import psutil

        proc = psutil.Process(job.pid)
        for filho in proc.children(recursive=True):
            filho.kill()  # o trabalho real costuma estar num neto (conda/docker)
        proc.kill()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "erro": f"nao consegui matar o PID {job.pid}: {exc}"}
    _ACORDA.set()
    return {"ok": True, "estado": CANCELADO}


def recursos() -> "Dict[str, Any]":
    """Foto do que a maquina tem agora + ocupacao por classe (para a tela)."""
    with _LOCK:
        ocupacao = {
            classe: {
                "limite": limite,
                "rodando": sum(
                    1 for j in _JOBS.values() if j.estado == RODANDO and j.classe == classe
                ),
                "na_fila": sum(
                    1 for j in _JOBS.values() if j.estado == NA_FILA and j.classe == classe
                ),
            }
            for classe, limite in LIMITES.items()
        }
    return {"ram": ram_livre(), "gpu": gpu_ocupada(), "classes": ocupacao}
