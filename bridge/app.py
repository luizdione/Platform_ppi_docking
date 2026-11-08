# -*- coding: utf-8 -*-
"""Ponte HTTP (FastAPI) entre o n8n e o funil do guarda-chuva.

Por que ela existe: o no *Execute Command* do n8n roda DENTRO do container, que
nao enxerga a GPU nem os ambientes conda do host. A ponte roda no HOST (env
do host) e o container fala com ela por
``http://host.docker.internal:<porta>``.

Rotas:

===========================  =============================================
GET  /health                 versao + toolchain (haddock3 disponivel?)
POST /score                  le scores de runs Boltz JA EXISTENTES
POST /gate                   aplica o GATE 1 sobre um bloco de scores
POST /gate/haddock           aplica o GATE 2 sobre as linhas do capri_clt
POST /haddock/prepare        split + interface + adapter.prepare (.cfg/.tbl)
POST /haddock/run            dispara o HADDOCK3 como job longo -> {job_id}
GET  /status/{job_id}        polling do job (o n8n fica aqui)
GET  /jobs                   lista os jobs deste processo
POST /funnel/run             score -> gate -> prepare de um subprojeto inteiro
===========================  =============================================

Contratos:

* Toda resposta e JSON serializavel puro (o n8n consome direto).
* Erro NUNCA vira 200 com resultado plausivel: vira 4xx/5xx com corpo
  ``{"error": "<mensagem util>"}``.
* **Papeis de cadeia vem da CAMPANHA, nunca da ordem das cadeias.** Os pedidos
  de ``/haddock/prepare``, ``/haddock/run`` e ``/funnel/run`` aceitam
  ``subproject``: com ele a ponte carrega o bloco ``chain_roles`` do
  ``config/subproject.*.yaml`` e o repassa ao funil. No DEMO01 isso e o que
  impede o ``receptor.pdb`` de sair com a BARSTAR num par cuja isca e a barnase
  (no `.cif`, A = barstar 760 res e B = barnase 376 res).
* A ponte **nao reimplementa** motor nenhum. Ela resolve, em tempo de chamada,
  as funcoes do pacote ``funnel`` (ver `_OPS` abaixo); se um modulo do funil
  ainda nao existe, responde **501** dizendo exatamente o que faltou - em vez
  de improvisar um resultado.

Porta padrao 8778 (env ``PPI_DOCK_BRIDGE_PORT``); o dashboard do guarda-chuva
usa 8777. A faixa 85xx foi ABANDONADA de proposito: com
a 8501 ja ocupada, o Streamlit auto-incrementa para a 8502 e
colide com a GUI daqui se as duas forem abertas juntas.
"""

from __future__ import annotations

import dataclasses
import importlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from funnel.contract import (
    GateResult,
    HaddockArtifacts,
    PairManifest,
    Scores,
)

from . import runners

__all__ = ["app", "porta_padrao"]

VERSAO = "0.1.0"


def porta_padrao() -> int:
    """Porta do servico (env PPI_DOCK_BRIDGE_PORT, default 8778)."""
    bruto = os.environ.get("PPI_DOCK_BRIDGE_PORT", "8778")
    try:
        return int(bruto)
    except ValueError as exc:
        raise ValueError(
            f"PPI_DOCK_BRIDGE_PORT invalida: {bruto!r} (esperava inteiro)"
        ) from exc


# --------------------------------------------------------------------------
# resolucao tardia das funcoes do funil
# --------------------------------------------------------------------------
# Cada operacao logica lista (modulo, [nomes aceitos]) em ordem de preferencia.
# A ponte NAO adivinha comportamento: se nada resolver, e 501 com a lista do que
# foi procurado.
_OPS: "Dict[str, List[tuple]]" = {
    # score_pair(subproject=..., pair_id=...) -> Scores | dict
    "score_pair": [
        ("funnel.scoring", ["score_pair", "score_run"]),
        ("funnel.score", ["score_pair", "score_run"]),
    ],
    # score_subproject(subproject=...) -> lista de PairManifest | dict
    "score_subproject": [
        ("funnel.scoring", ["score_subproject", "score_campaign", "score_all"]),
        ("funnel.score", ["score_subproject", "score_campaign", "score_all"]),
    ],
    # gate_boltz(scores, criteria=None) -> GateResult
    "gate_boltz": [("funnel.gate", ["gate_boltz"])],
    # gate_haddock(capri_rows, criteria=None) -> GateResult  (GATE 2)
    "gate_haddock": [("funnel.gate", ["gate_haddock"])],
    # prepare(manifest, workdir) -> HaddockArtifacts | PairManifest
    "prepare": [
        ("funnel.haddock_adapter", ["prepare"]),
        ("funnel.adapter", ["prepare", "prepare_pair", "prepare_haddock"]),
    ],
    # run(manifest, workdir) -> EngineResult | dict
    "run_haddock": [
        ("funnel.haddock_adapter", ["run"]),
        ("funnel.adapter", ["run", "run_pair", "run_haddock"]),
    ],
    # run_funnel(subproject=..., workdir=...) -> resumo dict (opcional)
    "run_funnel": [
        ("funnel.pipeline", ["run_funnel", "run_subproject", "run_campaign"]),
    ],
}


def _resolver(op: str, *, obrigatorio: bool = True):
    """Resolve a funcao do funil que implementa ``op``.

    Devolve o callable; se nao existir e ``obrigatorio``, levanta HTTP 501 com a
    lista exata de modulos/funcoes procurados (o operador sabe o que escrever).
    Com ``obrigatorio=False``, devolve None (usado quando ha caminho alternativo).
    """
    tentativas = _OPS[op]
    procurados = []
    for modulo, nomes in tentativas:
        procurados.extend(f"{modulo}.{n}" for n in nomes)
        try:
            mod = importlib.import_module(modulo)
        except ModuleNotFoundError as exc:
            # so "o modulo do funil nao existe" e motivo para tentar o proximo.
            # Se o que falta e uma DEPENDENCIA dele (numpy, biopython...), o
            # modulo EXISTE e esta quebrado: reportar 501 "nao implementado"
            # seria degradar em silencio e mandar o operador reescrever codigo
            # que ja esta la.
            if exc.name and (exc.name == modulo or modulo.startswith(f"{exc.name}.")):
                continue
            raise HTTPException(
                status_code=500,
                detail=(
                    f"'{modulo}' existe mas nao importou: falta a dependencia "
                    f"'{exc.name}' no ambiente da ponte ({exc})."
                ),
            ) from exc
        for nome in nomes:
            fn = getattr(mod, nome, None)
            if callable(fn):
                return fn
    if not obrigatorio:
        return None
    raise HTTPException(
        status_code=501,
        detail=(
            f"operacao '{op}' ainda nao implementada no pacote funnel. "
            f"Procurei por: {', '.join(procurados)}."
        ),
    )


def _json_safe(valor: Any) -> Any:
    """Converte objetos do contrato (e listas/dicts deles) para JSON puro."""
    if valor is None or isinstance(valor, (str, int, float, bool)):
        return valor
    if hasattr(valor, "to_dict"):
        return valor.to_dict()
    if isinstance(valor, dict):
        return {str(k): _json_safe(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple, set)):
        return [_json_safe(v) for v in valor]
    if dataclasses.is_dataclass(valor) and not isinstance(valor, type):
        # ex.: EngineResult do docking_suite_platform
        return {k: _json_safe(v) for k, v in dataclasses.asdict(valor).items()}
    if isinstance(valor, Path):
        return str(valor)
    raise TypeError(
        f"resposta nao serializavel: {type(valor).__name__} "
        "(o funil devolveu um objeto que a ponte nao sabe converter)"
    )


def _workdir(manifesto: PairManifest, informado: "str | None") -> str:
    """Resolve o diretorio de trabalho do HADDOCK3 para um par.

    Ordem: ``workdir`` do pedido -> ``PPI_DOCK_WORKDIR/<campanha>/<pair_id>``.
    Sem nenhum dos dois e **erro 422** - a ponte nao escolhe um destino
    plausivel no disco do usuario por conta propria.
    """
    if informado:
        return str(Path(informado))
    raiz = os.environ.get("PPI_DOCK_WORKDIR")
    if not raiz:
        raise HTTPException(
            status_code=422,
            detail=(
                "'workdir' ausente: informe o diretorio de trabalho no pedido ou "
                "defina a variavel de ambiente PPI_DOCK_WORKDIR na ponte."
            ),
        )
    return str(Path(raiz) / manifesto.campaign / manifesto.pair_id)


def _chain_roles(subproject: "str | None") -> "Dict[str, Any] | None":
    """Papeis de cadeia DECLARADOS pela campanha (``config/subproject.*.yaml``).

    Sem ``subproject`` no pedido nao ha o que carregar (``None``) e o funil cai
    no fallback POSICIONAL do motor - que no DEMO01 poria a BARSTAR no
    ``receptor.pdb`` de um par cuja isca e a barnase. Com ele, os papeis vem
    declarados e sao conferidos contra o modelo.

    O leitor e ``core.data`` (que le os YAMLs de campanha e NAO depende de
    streamlit); campanha desconhecida vira 422, nunca um preparo com papel
    chutado.
    """
    if not subproject:
        return None
    from core.data import GuiError, chain_roles_for

    try:
        bloco = chain_roles_for(subproject)
    except GuiError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return bloco or None


def _com_motor(fn, kwargs: "Dict[str, Any]", motor: "str | None"):
    """Acrescenta ``motor`` aos kwargs SE a implementacao aceitar.

    ``motor`` escolhe onde o HADDOCK3 roda ('local' | 'docker' | 'auto'). Se o
    chamador pediu um motor explicito e a implementacao nao o suporta, e 501:
    rodar no motor errado (ex.: cair no binario local quando se pediu container)
    daria um resultado que o usuario nao pediu, sem aviso.
    """
    if not motor:
        return kwargs
    import inspect

    try:
        aceita = "motor" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        aceita = False
    if not aceita:
        raise HTTPException(
            status_code=501,
            detail=(
                f"a implementacao do funil ({getattr(fn, '__module__', '?')}."
                f"{getattr(fn, '__name__', '?')}) nao aceita 'motor'; nao vou "
                f"rodar num motor diferente do pedido ({motor!r})."
            ),
        )
    return {**kwargs, "motor": motor}


def _chamar_com_papeis(fn, *args, chain_roles: "Dict[str, Any] | None",
                       motor: "str | None" = None):
    """Chama ``fn`` repassando ``chain_roles`` SE a implementacao aceitar.

    O funil e resolvido em tempo de chamada (ver ``_OPS``): uma implementacao
    alternativa pode nao ter o parametro. Nesse caso, com papeis declarados em
    maos, e 501 explicito - preparar o par ignorando a declaracao da campanha
    seria justamente o bug dos papeis trocados, agora em silencio.
    """
    if chain_roles is None:
        return fn(*args, **_com_motor(fn, {}, motor))
    import inspect

    try:
        aceita = "chain_roles" in inspect.signature(fn).parameters
    except (TypeError, ValueError):  # builtins/callables sem assinatura
        aceita = False
    if not aceita:
        raise HTTPException(
            status_code=501,
            detail=(
                f"a implementacao do funil ({getattr(fn, '__module__', '?')}."
                f"{getattr(fn, '__name__', '?')}) nao aceita 'chain_roles'; nao vou "
                "preparar o par ignorando os papeis declarados pela campanha."
            ),
        )
    return fn(*args, **_com_motor(fn, {"chain_roles": chain_roles}, motor))


def _como_manifesto(data: Any) -> PairManifest:
    """Aceita PairManifest ou dict e devolve sempre PairManifest."""
    if isinstance(data, PairManifest):
        return data
    if isinstance(data, dict):
        return PairManifest.from_dict(data)
    raise TypeError(
        f"esperava manifesto (dict ou PairManifest), veio {type(data).__name__}"
    )


# --------------------------------------------------------------------------
# corpos das requisicoes
# --------------------------------------------------------------------------
class ScoreRequest(BaseModel):
    """Le scores de runs Boltz JA EXISTENTES (nao dispara predicao nenhuma)."""

    subproject: str = Field(..., description="campanha/subprojeto, ex.: DEMO01_barnase_barstar")
    pair_id: "Optional[str]" = Field(
        None, description="label do run Boltz; ausente = todos os pares"
    )


class GateRequest(BaseModel):
    """Aplica o GATE 1 (pos-Boltz) sobre um bloco de scores."""

    scores: "Dict[str, Any]" = Field(..., description="campos de funnel.contract.Scores")
    criteria: "Optional[Dict[str, Any]]" = Field(
        None, description="limiares para sobrescrever os padroes do funil"
    )


class GateHaddockRequest(BaseModel):
    """Aplica o GATE 2 (pos-HADDOCK3) sobre as linhas do caprieval por cluster."""

    capri_rows: "List[Dict[str, Any]]" = Field(
        ..., description="linhas do capri_clt.tsv (uma por cluster)"
    )
    criteria: "Optional[Dict[str, Any]]" = None


class PrepareRequest(BaseModel):
    """Prepara o HADDOCK3 de um par (logica pura: nao exige o binario)."""

    manifest: "Dict[str, Any]" = Field(..., description="PairManifest serializado")
    workdir: "Optional[str]" = Field(None, description="destino do .cfg/ambig.tbl")
    subproject: "Optional[str]" = Field(
        None,
        description=(
            "campanha (key ou campaign de config/subproject.*.yaml). Faz a ponte "
            "carregar o bloco 'chain_roles' declarado e repassa-lo ao funil, para "
            "receptor/ligante nao sairem pela ORDEM das cadeias do .cif"
        ),
    )


class RunRequest(BaseModel):
    """Dispara o HADDOCK3 de um par como job longo."""

    manifest: "Dict[str, Any]" = Field(..., description="PairManifest serializado")
    workdir: "Optional[str]" = None
    subproject: "Optional[str]" = Field(
        None, description="campanha de onde vem o bloco 'chain_roles' declarado"
    )
    motor: "Optional[str]" = Field(
        None,
        description=(
            "onde rodar o HADDOCK3: 'docker' (container Linux), 'local' (binario "
            "no PATH) ou 'auto' (local se houver, senao container). Omitido = "
            "'auto'. No Windows o caminho normal e 'docker': o CNS, motor de "
            "calculo do HADDOCK3, nao tem build para Windows"
        ),
    )


class FunnelRequest(BaseModel):
    """Pipeline score -> gate -> prepare de um subprojeto inteiro."""

    subproject: str
    workdir: "Optional[str]" = None
    criteria: "Optional[Dict[str, Any]]" = None
    background: bool = Field(
        False, description="True devolve {job_id} em vez de esperar o resultado"
    )


# --------------------------------------------------------------------------
# aplicacao
# --------------------------------------------------------------------------
app = FastAPI(
    title="ppi_docking_platform - ponte HTTP",
    version=VERSAO,
    description="Ponte entre o n8n (container) e o funil Boltz-2 -> HADDOCK3 (host).",
)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|host\.docker\.internal)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    """Padroniza o corpo de erro em {"error": ...} (o n8n le esse campo)."""
    detalhe = exc.detail
    if isinstance(detalhe, dict) and "error" in detalhe:
        corpo = detalhe
    else:
        corpo = {"error": detalhe if isinstance(detalhe, str) else str(detalhe)}
    return JSONResponse(status_code=exc.status_code, content=corpo)


@app.exception_handler(RequestValidationError)
async def _erro_validacao(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Corpo invalido tambem responde {"error": ...}.

    Sem isso o FastAPI devolveria o formato dele ({"detail": [...]}) e o n8n,
    que le sempre ``$json.error``, veria um erro sem mensagem.
    """
    partes = [
        f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg')}"
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": "corpo invalido - " + "; ".join(partes), "detail": partes},
    )


@app.exception_handler(Exception)
async def _erro_interno(_: Request, exc: Exception) -> JSONResponse:
    """Qualquer excecao nao tratada vira 500 com mensagem util - nunca 200."""
    return JSONResponse(
        status_code=500, content={"error": f"{type(exc).__name__}: {exc}"}
    )


def _haddock3_disponivel() -> "Dict[str, Any]":
    """Estado do binario haddock3, SEM mascarar a causa da indisponibilidade.

    Preferencia: ``funnel.haddock_adapter.check_toolchain`` (mesma resolucao que
    o `run` vai usar). Fallback: PATH. Se nem isso der, o /health reporta o
    motivo em ``detail`` em vez de dizer apenas "False".
    """
    import shutil

    try:
        adapter = importlib.import_module("funnel.haddock_adapter")
        info = adapter.check_toolchain()
        disponivel = bool(info.get("available"))
        # o motor devolve {"available":..., "tools": {"haddock3": {"path": ...}}} -
        # nao ha 'path' na raiz; ler so a raiz reportaria None mesmo instalado.
        entrada = (info.get("tools") or {}).get("haddock3") or {}
        caminho = info.get("path") or entrada.get("path")
        return {"available": disponivel, "path": caminho, "detail": info}
    except Exception as exc:  # noqa: BLE001 - ausencia do adapter nao derruba /health
        caminho = shutil.which("haddock3")
        return {
            "available": bool(caminho),
            "path": caminho,
            "detail": f"check_toolchain indisponivel ({type(exc).__name__}: {exc}); "
            "usei apenas o PATH",
        }


@app.get("/health")
def health() -> "Dict[str, Any]":
    """Sanidade da ponte + estado da toolchain externa."""
    hd = _haddock3_disponivel()
    return {
        "ok": True,
        "versao": VERSAO,
        "porta": porta_padrao(),
        "jobs_dir": str(runners.jobs_dir()),
        "toolchain": {
            "haddock3": bool(hd["available"]),
            "haddock3_path": hd.get("path"),
            "haddock3_detail": _json_safe(hd.get("detail")),
        },
    }


@app.post("/score")
def score(req: ScoreRequest) -> "Dict[str, Any]":
    """Le os scores de runs do Boltz JA EXISTENTES (nao re-prediz nada).

    Campanha declarada cujos DADOS nao estao nesta maquina (planilha de
    scores ou raiz de resultados ausente) e 422, nao 500: quem chamou
    aponta para um disco que nao existe aqui, e a mensagem diz qual
    caminho faltou. 500 fica para defeito de verdade.
    """
    from core.data import GuiError

    try:
        if req.pair_id:
            fn = _resolver("score_pair")
            bruto = fn(subproject=req.subproject, pair_id=req.pair_id)
            return {
                "subproject": req.subproject,
                "pair_id": req.pair_id,
                "scores": _json_safe(bruto),
            }
        fn = _resolver("score_subproject")
        bruto = fn(subproject=req.subproject)
    except GuiError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if type(exc).__name__ == "ScoringError":
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        raise
    itens = _json_safe(bruto)
    if not isinstance(itens, list):
        itens = [itens]
    return {"subproject": req.subproject, "n": len(itens), "pairs": itens}


@app.post("/gate")
def gate(req: GateRequest) -> "Dict[str, Any]":
    """GATE 1: aplica o funil de confianca sobre um bloco de scores."""
    try:
        scores = Scores.from_dict(req.scores)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"scores invalidos: {exc}") from exc
    fn = _resolver("gate_boltz")
    try:
        veredicto = fn(scores, criteria=req.criteria) if req.criteria else fn(scores)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"criterios do GATE 1 invalidos: {exc}"
        ) from exc
    if not isinstance(veredicto, GateResult):
        raise HTTPException(
            status_code=500,
            detail=(
                "funnel.gate.gate_boltz devolveu "
                f"{type(veredicto).__name__}, esperava GateResult"
            ),
        )
    return veredicto.to_dict()


@app.post("/gate/haddock")
def gate_haddock(req: GateHaddockRequest) -> "Dict[str, Any]":
    """GATE 2: aplica o funil pos-docking sobre as linhas do `capri_clt`."""
    fn = _resolver("gate_haddock")
    try:
        veredicto = (
            fn(req.capri_rows, criteria=req.criteria)
            if req.criteria
            else fn(req.capri_rows)
        )
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422, detail=f"entrada do GATE 2 invalida: {exc}"
        ) from exc
    if not isinstance(veredicto, GateResult):
        raise HTTPException(
            status_code=500,
            detail=(
                "funnel.gate.gate_haddock devolveu "
                f"{type(veredicto).__name__}, esperava GateResult"
            ),
        )
    return veredicto.to_dict()


@app.post("/haddock/prepare")
def haddock_prepare(req: PrepareRequest) -> "Dict[str, Any]":
    """Split das cadeias + residuos de interface + geracao de .cfg/ambig.tbl.

    Logica PURA (nao exige o binario haddock3 instalado).
    """
    try:
        manifesto = _como_manifesto(req.manifest)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"manifesto invalido: {exc}") from exc
    fn = _resolver("prepare")
    wd = _workdir(manifesto, req.workdir)
    papeis = _chain_roles(req.subproject)
    try:
        saida = _chamar_com_papeis(fn, manifesto, wd, chain_roles=papeis)
    except (ValueError, FileNotFoundError) as exc:
        # manifesto incompleto (sem PDBs do split, sem residuos ativos, ...) e
        # erro do PEDIDO, nao do servidor: 422 para o n8n distinguir do 5xx.
        raise HTTPException(
            status_code=422, detail=f"manifesto insuficiente para o HADDOCK3: {exc}"
        ) from exc
    if isinstance(saida, PairManifest):
        return {"manifest": saida.to_dict(), "haddock": saida.haddock.to_dict()}
    if isinstance(saida, HaddockArtifacts):
        manifesto.haddock = saida
        manifesto.stage = "prepared"
        return {"manifest": manifesto.to_dict(), "haddock": saida.to_dict()}
    raise HTTPException(
        status_code=500,
        detail=(
            f"funnel prepare devolveu {type(saida).__name__}; "
            "esperava PairManifest ou HaddockArtifacts"
        ),
    )


def _job_haddock(
    *,
    log,
    manifesto: "Dict[str, Any]",
    workdir: str,
    chain_roles: "Dict[str, Any] | None" = None,
    motor: "str | None" = None,
) -> "Dict[str, Any]":
    """Corpo do job longo do HADDOCK3 (roda em thread, ver runners.start_job)."""
    m = _como_manifesto(manifesto)
    log(f"par {m.pair_id} (campanha {m.campaign}) em {workdir}")
    fn = _resolver("run_haddock")
    log(
        "chamando o funil (haddock_adapter.run) com papeis "
        + ("DECLARADOS pela campanha" if chain_roles else "pela convencao posicional")
        + f"; motor={motor or 'auto'}"
    )
    saida = _chamar_com_papeis(fn, m, workdir, chain_roles=chain_roles, motor=motor)
    resultado = _json_safe(saida)
    status = resultado.get("status") if isinstance(resultado, dict) else None
    if status and status != "ok":
        # o motor reporta falha no proprio envelope: o job NAO pode virar "done"
        raise RuntimeError(
            f"HADDOCK3 falhou (status={status}): "
            f"{resultado.get('error') or 'sem mensagem do motor'}"
        )
    log("funil devolveu resultado")
    return resultado


@app.post("/haddock/run")
def haddock_run(req: RunRequest) -> "Dict[str, Any]":
    """Dispara o HADDOCK3 como job longo e devolve {job_id} para polling.

    Quando o binario haddock3 nao esta instalado (o caso no Windows), o job
    termina em status "error" com a mensagem do motor - jamais em "done".
    """
    try:
        m = _como_manifesto(req.manifest)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"manifesto invalido: {exc}") from exc
    # resolve ANTES de abrir a thread, para 501/422 chegarem como HTTP e nao como job morto
    _resolver("run_haddock")
    wd = _workdir(m, req.workdir)
    papeis = _chain_roles(req.subproject)  # 422 aqui, nao dentro da thread
    job = runners.start_job(
        _job_haddock,
        name=f"haddock3:{m.pair_id}",
        manifesto=m.to_dict(),
        workdir=wd,
        chain_roles=papeis,
        motor=req.motor,
    )
    return {"job_id": job.job_id, "status": job.status, "log_path": job.log_path}


@app.get("/status/{job_id}")
def status(job_id: str) -> "Dict[str, Any]":
    """Estado de um job (o n8n faz polling aqui)."""
    job = runners.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"job '{job_id}' desconhecido neste processo da ponte "
                "(o registro e em memoria; reiniciar o servico apaga os jobs)"
            ),
        )
    return job.to_dict()


@app.get("/jobs")
def jobs() -> "Dict[str, Any]":
    """Lista os jobs conhecidos por este processo."""
    itens = runners.list_jobs()
    return {"n": len(itens), "jobs": itens}


def _funil_completo(
    *, subproject: str, workdir: "str | None", criteria: "Dict[str, Any] | None", log=None
) -> "Dict[str, Any]":
    """score -> gate -> prepare para todos os pares de um subprojeto.

    Prefere ``funnel.pipeline.run_funnel`` quando existir; senao compoe as tres
    operacoes aqui. Erro em UM par nao contamina os demais: o par entra em
    ``erros`` com a mensagem, e o resumo mostra a contagem real.
    """
    def _log(t: str) -> None:
        if log:
            log(t)

    pipeline = _resolver("run_funnel", obrigatorio=False)
    if pipeline is not None:
        _log("delegando a funnel.pipeline")
        return _json_safe(pipeline(subproject=subproject, workdir=workdir, criteria=criteria))

    fn_score = _resolver("score_subproject")
    fn_gate = _resolver("gate_boltz")
    fn_prep = _resolver("prepare")
    # papeis declarados pela campanha: entram no prepare de CADA par aprovado.
    papeis = _chain_roles(subproject)
    _log(
        "papeis de cadeia: "
        + ("declarados no YAML da campanha" if papeis else "convencao posicional do motor")
    )

    _log(f"lendo scores de {subproject}")
    itens = fn_score(subproject=subproject)
    if not isinstance(itens, (list, tuple)):
        itens = [itens]

    aprovados: "List[Dict[str, Any]]" = []
    reprovados: "List[Dict[str, Any]]" = []
    erros: "List[Dict[str, Any]]" = []

    for item in itens:
        try:
            m = _como_manifesto(_json_safe(item))
            veredicto = fn_gate(m.scores, criteria=criteria) if criteria else fn_gate(m.scores)
            if not isinstance(veredicto, GateResult):
                raise TypeError(
                    f"gate_boltz devolveu {type(veredicto).__name__}, esperava GateResult"
                )
            m.gate_boltz = veredicto
            if not veredicto.approved:
                m.stage = "rejected"
                reprovados.append(m.to_dict())
                _log(f"{m.pair_id}: REPROVADO ({'; '.join(veredicto.reasons)})")
                continue
            m.stage = "gated"
            _log(f"{m.pair_id}: aprovado; preparando HADDOCK3")
            saida = _chamar_com_papeis(
                fn_prep, m, _workdir(m, workdir), chain_roles=papeis
            )
            if isinstance(saida, PairManifest):
                m = saida
            elif isinstance(saida, HaddockArtifacts):
                m.haddock = saida
                m.stage = "prepared"
            else:
                raise TypeError(
                    f"prepare devolveu {type(saida).__name__}; "
                    "esperava PairManifest ou HaddockArtifacts"
                )
            aprovados.append(m.to_dict())
        except Exception as exc:  # noqa: BLE001 - um par ruim nao derruba o lote
            pid = item.get("pair_id") if isinstance(item, dict) else getattr(item, "pair_id", "?")
            erros.append({"pair_id": pid, "error": f"{type(exc).__name__}: {exc}"})
            _log(f"{pid}: ERRO {exc}")

    return {
        "subproject": subproject,
        "n_total": len(itens),
        "n_aprovados": len(aprovados),
        "n_reprovados": len(reprovados),
        "n_erros": len(erros),
        "aprovados": aprovados,
        "reprovados": reprovados,
        "erros": erros,
    }


@app.post("/funnel/run")
def funnel_run(req: FunnelRequest) -> "Dict[str, Any]":
    """Pipeline inteiro de um subprojeto: score -> gate -> prepare.

    Com ``background=true`` devolve ``{job_id}`` (use /status/{job_id}).
    """
    if req.background:
        job = runners.start_job(
            _funil_completo,
            name=f"funnel:{req.subproject}",
            subproject=req.subproject,
            workdir=req.workdir,
            criteria=req.criteria,
        )
        return {"job_id": job.job_id, "status": job.status, "log_path": job.log_path}
    return _funil_completo(
        subproject=req.subproject, workdir=req.workdir, criteria=req.criteria
    )


if __name__ == "__main__":  # pragma: no cover - execucao manual do servico
    import uvicorn

    uvicorn.run(
        "bridge.app:app",
        host=os.environ.get("PPI_DOCK_BRIDGE_HOST", "0.0.0.0"),
        port=porta_padrao(),
        reload=False,
    )
