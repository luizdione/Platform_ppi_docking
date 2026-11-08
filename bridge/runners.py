# -*- coding: utf-8 -*-
"""Execucao de processos externos e registro de jobs longos da ponte.

Este modulo NAO conhece o funil: ele so sabe (a) disparar um subprocesso com
log capturado e (b) guardar o estado de um trabalho demorado para o n8n fazer
polling em `/status/{job_id}`.

Regras:

* **Nunca** `shell=True` - todo comando e uma lista de argumentos.
* **Nunca** degradar em silencio: binario ausente, timeout ou codigo de saida
  != 0 viram resultado explicito (`returncode`/`timeout`/`error`), jamais um
  "ok" plausivel.
* O registro de jobs vive **em memoria** (dict protegido por lock) e o log vai
  para disco, porque o processo da ponte pode ser reiniciado e o n8n ainda
  precisa achar o arquivo de log.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List

__all__ = [
    "DEFAULT_TIMEOUT",
    "jobs_dir",
    "conda_exe",
    "run_cmd",
    "run_ppi",
    "Job",
    "start_job",
    "get_job",
    "list_jobs",
]

# Timeout padrao de um comando sincrono (segundos). Jobs longos usam start_job.
DEFAULT_TIMEOUT: float = float(os.environ.get("PPI_DOCK_TIMEOUT", "900"))

# Nome do ambiente conda onde vive o CLI externo de analise, quando a ponte
# precisa chama-lo por `conda run`. Configuravel pela env PPI_DOCK_ENV.
DEFAULT_ENV: str = os.environ.get("PPI_DOCK_ENV", "base")

# Modulo python do CLI externo (mesma env que o `web/runner.py` usa).
ENGINE_CLI_ENV: str = "PPI_ENGINE_CLI"


def jobs_dir() -> Path:
    """Diretorio onde ficam os logs dos jobs (env PPI_DOCK_JOBS_DIR)."""
    raiz = os.environ.get("PPI_DOCK_JOBS_DIR")
    destino = Path(raiz) if raiz else Path.home() / ".ppi_docking_jobs"
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def _agora() -> str:
    """Instante atual em ISO-8601 UTC (JSON-safe)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# execucao de subprocessos
# --------------------------------------------------------------------------
def conda_exe() -> str:
    """Resolve o executavel do conda. Ausencia vira erro claro, nunca fallback.

    Ordem: env PPI_DOCK_CONDA -> PATH (`conda`/`mamba`).
    """
    forcado = os.environ.get("PPI_DOCK_CONDA")
    if forcado:
        if not Path(forcado).exists():
            raise FileNotFoundError(
                f"PPI_DOCK_CONDA aponta para um caminho inexistente: {forcado}"
            )
        return forcado
    for nome in ("conda", "mamba"):
        hit = shutil.which(nome)
        if hit:
            return hit
    raise FileNotFoundError(
        "executavel 'conda' nao encontrado no PATH. Defina PPI_DOCK_CONDA com o "
        "caminho completo (no Windows, tipicamente <raiz do miniforge>\\condabin\\conda.bat)."
    )


def run_cmd(
    cmd: "List[str]",
    *,
    cwd: "str | Path | None" = None,
    timeout: "float | None" = None,
    env: "Dict[str, str] | None" = None,
) -> "Dict[str, Any]":
    """Roda um comando (lista de argumentos) capturando stdout/stderr.

    Devolve dict JSON-safe: ``{cmd, returncode, stdout, stderr, duration_s,
    timeout}``. ``returncode`` vira ``None`` quando houve timeout (e
    ``timeout=True``). Binario inexistente levanta ``FileNotFoundError``.
    """
    if not isinstance(cmd, (list, tuple)) or not cmd:
        raise TypeError("run_cmd: esperava uma lista NAO vazia de argumentos")
    argv = [str(a) for a in cmd]
    limite = DEFAULT_TIMEOUT if timeout is None else float(timeout)
    ambiente = None
    if env:
        ambiente = {**os.environ, **{str(k): str(v) for k, v in env.items()}}
    t0 = time.time()
    try:
        proc = subprocess.run(  # noqa: S603 - argv explicito, sem shell
            argv,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=limite,
            shell=False,
            env=ambiente,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": argv,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "")
            + f"\n[ponte] TIMEOUT apos {limite:g}s: {' '.join(argv)}",
            "duration_s": round(time.time() - t0, 3),
            "timeout": True,
        }
    return {
        "cmd": argv,
        "returncode": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "duration_s": round(time.time() - t0, 3),
        "timeout": False,
    }


def run_ppi(
    args: "List[str]",
    env_name: str = DEFAULT_ENV,
    *,
    cwd: "str | Path | None" = None,
    timeout: "float | None" = None,
) -> "Dict[str, Any]":
    """Chama o CLI EXTERNO de analise dentro de um env conda.

    Equivale a ``conda run -n <env> --no-capture-output python -m <cli> <args>``,
    onde ``<cli>`` e o modulo declarado na env ``PPI_ENGINE_CLI``. A ponte NAO
    reimplementa nada desse motor, e tambem nao o embute: sem a env, erra alto.
    """
    if not isinstance(args, (list, tuple)):
        raise TypeError("run_ppi: 'args' precisa ser lista de argumentos da CLI")
    cli = os.environ.get(ENGINE_CLI_ENV, "").strip()
    if not cli:
        raise RuntimeError(
            f"{ENGINE_CLI_ENV} nao definida: nao ha CLI externo de analise "
            "configurado. O funil (score/gate/HADDOCK3) nao depende dela."
        )
    argv = [
        conda_exe(),
        "run",
        "-n",
        str(env_name),
        "--no-capture-output",
        "python",
        "-m",
        cli,
        *[str(a) for a in args],
    ]
    return run_cmd(argv, cwd=cwd, timeout=timeout)


# --------------------------------------------------------------------------
# registro de jobs longos (HADDOCK3, funil inteiro)
# --------------------------------------------------------------------------
@dataclass
class Job:
    """Estado de um trabalho longo rodando em thread.

    ``status`` in {"running", "done", "error"}. ``result`` so e preenchido em
    "done"; ``error`` so em "error" - nunca os dois.
    """

    job_id: str
    name: str
    status: str = "running"
    started: str = field(default_factory=_agora)
    finished: "str | None" = None
    log_path: "str | None" = None
    result: "Any | None" = None
    error: "str | None" = None

    def to_dict(self) -> "Dict[str, Any]":
        """Serializa para dict JSON-safe (o n8n consome isso no polling)."""
        return asdict(self)


_JOBS: "Dict[str, Job]" = {}
_LOCK = threading.Lock()


def _escrever_log(caminho: Path, texto: str) -> None:
    """Anexa uma linha ao log do job (sempre com timestamp)."""
    with caminho.open("a", encoding="utf-8") as fh:
        fh.write(f"[{_agora()}] {texto}\n")


def start_job(fn: "Callable[..., Any]", *, name: str = "job", **kwargs: Any) -> Job:
    """Dispara ``fn`` numa thread e devolve o Job (status inicial "running").

    ``fn`` recebe os ``kwargs`` informados mais ``log`` - uma funcao
    ``log(texto)`` que grava no arquivo de log do job. O valor de retorno de
    ``fn`` precisa ser JSON serializavel (vai direto para o n8n).

    Qualquer excecao dentro de ``fn`` marca o job como "error" com o traceback
    no log - a ponte nunca reporta sucesso de um job que quebrou.
    """
    if not callable(fn):
        raise TypeError("start_job: 'fn' precisa ser chamavel")
    job_id = uuid.uuid4().hex[:12]
    log_path = jobs_dir() / f"{job_id}.log"
    job = Job(job_id=job_id, name=str(name), log_path=str(log_path))
    with _LOCK:
        _JOBS[job_id] = job
    _escrever_log(log_path, f"job {job_id} ({name}) iniciado")

    def _log(texto: str) -> None:
        _escrever_log(log_path, str(texto))

    def _alvo() -> None:
        try:
            resultado = fn(log=_log, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - o job nao pode morrer calado
            _escrever_log(log_path, "FALHOU:\n" + traceback.format_exc())
            with _LOCK:
                job.status = "error"
                job.error = f"{type(exc).__name__}: {exc}"
                job.finished = _agora()
            return
        _escrever_log(log_path, "concluido")
        with _LOCK:
            job.status = "done"
            job.result = resultado
            job.finished = _agora()

    threading.Thread(target=_alvo, name=f"ppi-dock-{job_id}", daemon=True).start()
    return job


def get_job(job_id: str) -> "Job | None":
    """Devolve o Job pelo id, ou None se nao existir neste processo."""
    with _LOCK:
        return _JOBS.get(str(job_id))


def list_jobs() -> "List[Dict[str, Any]]":
    """Lista os jobs conhecidos, do mais recente para o mais antigo."""
    with _LOCK:
        todos = list(_JOBS.values())
    return [j.to_dict() for j in sorted(todos, key=lambda j: j.started, reverse=True)]
