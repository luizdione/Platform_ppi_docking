# -*- coding: utf-8 -*-
"""Rotas da fila de jobs: disparar, acompanhar, ver o log ao vivo e cancelar.

O log ao vivo usa **SSE** (`text/event-stream`), e nao WebSocket: o log e um
fluxo append-only de mao unica, o `EventSource` do navegador reconecta sozinho e
tudo passa por HTTP simples. WebSocket traria bidirecionalidade que nao usamos.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from web import jobs as motor

router = APIRouter()

RAIZ = Path(__file__).resolve().parent.parent.parent


class PedidoFunil(BaseModel):
    subprojeto: str
    ate: str = "prepare"


class PedidoBoltz(BaseModel):
    inputs: str
    results: str
    diffusion_samples: "int | None" = None


class PedidoHaddock(BaseModel):
    subprojeto: str


class PedidoMD(BaseModel):
    run: str
    md_dir: str
    tier: str = "fast"
    ff: str = "charmm36m"
    ns: "float | None" = None
    replicas: "int | None" = None
    platform: "str | None" = None
    #: `False` (padrao) RETOMA a MD de onde parou -- MD dura dias e e resumivel.
    fresh: bool = False


class PedidoEnergia(BaseModel):
    alvo: str
    pose: bool = False
    saida: str = ""
    frames: int = 50
    ff: str = "ff19SB"
    gb: str = "obc2"
    platform: "str | None" = None


class PedidoSequencia(BaseModel):
    """Esteira: as etapas escolhidas, em ordem, cada uma so apos a anterior."""

    subprojeto: str
    #: subconjunto de ETAPAS_SEQUENCIA, na ordem do funil
    etapas: "List[str]"
    #: parametros por etapa (ex.: {"md": {"run": ..., "md_dir": ...}})
    parametros: "Dict[str, Dict[str, Any]]" = {}


#: A cascata canonica da plataforma, na ordem em que faz sentido cientificamente.
#: `classe` e o recurso que a etapa consome -- e o que a fila usa para nao deixar
#: dois jobs pesados juntos na mesma maquina.
ETAPAS_SEQUENCIA = [
    {
        "id": "boltz",
        "titulo": "Predicao de complexo (Boltz-2)",
        "classe": "gpu",
        "descricao": "co-dobramento isca x parceiro; exige GPU (CUDA).",
    },
    {
        "id": "funil",
        "titulo": "Funil de confianca (GATE)",
        "classe": "leve",
        "descricao": "pontua e aprova quem segue (ipTM/ipSAE/PAE + cobertura).",
    },
    {
        "id": "haddock",
        "titulo": "Docking fisico (HADDOCK3)",
        "classe": "cpu_heavy",
        "descricao": "confirmacao independente da interface, em container; horas.",
    },
    {
        "id": "md",
        "titulo": "Dinamica molecular (MD)",
        "classe": "gpu",
        "descricao": "estabilidade da interface em solvente explicito; dias, resumivel.",
    },
    {
        "id": "energia",
        "titulo": "Energia livre (MM/GBSA)",
        "classe": "gpu",
        "descricao": "dG de ligacao, sobre a trajetoria da MD ou direto na pose.",
    },
]
_ETAPAS_POR_ID = {e["id"]: e for e in ETAPAS_SEQUENCIA}


@router.get("/jobs")
def listar_jobs(limite: int = 50) -> "Dict[str, Any]":
    """Fila + historico (mais novos primeiro) e o estado dos recursos."""
    return {"jobs": motor.listar(limite), "recursos": motor.recursos()}


@router.get("/jobs/{job_id}")
def ver_job(job_id: str) -> "Dict[str, Any]":
    job = motor.obter(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job desconhecido: {job_id!r}")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancelar")
def cancelar_job(job_id: str) -> "Dict[str, Any]":
    resultado = motor.cancelar(job_id)
    if not resultado.get("ok"):
        raise HTTPException(status_code=409, detail=resultado.get("erro", "falhou"))
    return resultado


@router.get("/jobs/{job_id}/log")
async def log_ao_vivo(job_id: str) -> StreamingResponse:
    """Log do job em SSE: manda o que ja existe e segue acompanhando o arquivo."""
    job = motor.obter(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job desconhecido: {job_id!r}")

    async def fluxo():
        caminho = job.log_path
        posicao = 0
        ocioso = 0.0
        while True:
            texto = ""
            if caminho.is_file():
                try:
                    with open(caminho, "r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(posicao)
                        texto = fh.read()
                        posicao = fh.tell()
                except OSError:
                    texto = ""
            if texto:
                ocioso = 0.0
                for linha in texto.splitlines():
                    yield f"data: {json.dumps(linha, ensure_ascii=False)}\n\n"
            else:
                ocioso += 1.0

            atual = motor.obter(job_id)
            if atual is not None and atual.estado not in motor.VIVOS:
                # manda o desfecho e encerra o fluxo
                yield (
                    "event: fim\ndata: "
                    + json.dumps(
                        {"estado": atual.estado, "rc": atual.rc, "erro": atual.erro},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
                return
            if ocioso > 900:  # 15 min sem nada e sem terminar: solta o cliente
                yield "event: fim\ndata: {\"estado\":\"ocioso\"}\n\n"
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(
        fluxo(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/recursos")
def ver_recursos() -> "Dict[str, Any]":
    """RAM, GPU e ocupacao por classe -- o que o preflight olha antes de admitir."""
    return motor.recursos()


# ---------------------------------------------------------------------------
# acoes (cada uma vira um job na fila)
# ---------------------------------------------------------------------------
@router.post("/acoes/funil")
def rodar_funil(pedido: PedidoFunil) -> "Dict[str, Any]":
    """Enfileira o funil de confianca de um subprojeto (classe leve)."""
    job = motor.enfileirar(
        f"funil {pedido.subprojeto} (ate {pedido.ate})",
        [sys.executable, "-m", "web.runner", "funil", pedido.subprojeto,
         "--ate", pedido.ate],
        classe="leve",
        cwd=str(RAIZ),
        meta={"acao": "funil", "subprojeto": pedido.subprojeto, "ate": pedido.ate},
    )
    return {"job_id": job.id, "estado": job.estado}


def _cmd_etapa(etapa: str, subprojeto: str, p: "Dict[str, Any]") -> "List[str]":
    """Monta o argv de UMA etapa. Fonte unica: a etapa avulsa e a etapa dentro
    da esteira usam exatamente o mesmo comando -- nao pode haver dois jeitos de
    rodar a mesma coisa, senao um deles envelhece escondido."""
    base = [sys.executable, "-m", "web.runner"]
    if etapa == "boltz":
        faltando = [c for c in ("inputs", "results") if not p.get(c)]
        if faltando:
            raise HTTPException(
                status_code=422,
                detail=f"etapa 'boltz' exige {', '.join(faltando)}",
            )
        cmd = base + ["boltz", p["inputs"], p["results"]]
        if p.get("diffusion_samples"):
            cmd += ["--diffusion-samples", str(p["diffusion_samples"])]
        return cmd
    if etapa == "funil":
        return base + ["funil", subprojeto, "--ate", p.get("ate", "prepare")]
    if etapa == "haddock":
        return base + ["haddock", subprojeto]
    if etapa == "md":
        faltando = [c for c in ("run", "md_dir") if not p.get(c)]
        if faltando:
            raise HTTPException(
                status_code=422, detail=f"etapa 'md' exige {', '.join(faltando)}"
            )
        cmd = base + [
            "md", p["run"], p["md_dir"],
            "--tier", p.get("tier", "fast"),
            "--ff", p.get("ff", "charmm36m"),
        ]
        if p.get("ns"):
            cmd += ["--ns", str(p["ns"])]
        if p.get("replicas"):
            cmd += ["--replicas", str(p["replicas"])]
        if p.get("platform"):
            cmd += ["--platform", p["platform"]]
        if p.get("fresh"):
            cmd.append("--fresh")
        return cmd
    if etapa == "energia":
        if not p.get("alvo"):
            raise HTTPException(status_code=422, detail="etapa 'energia' exige alvo")
        cmd = base + ["energia", p["alvo"]]
        if p.get("pose"):
            cmd += ["--pose", "--saida", p.get("saida", "")]
        else:
            cmd += ["--frames", str(p.get("frames", 50))]
        cmd += ["--ff", p.get("ff", "ff19SB"), "--gb", p.get("gb", "obc2")]
        if p.get("platform"):
            cmd += ["--platform", p["platform"]]
        return cmd
    raise HTTPException(status_code=422, detail=f"etapa desconhecida: {etapa!r}")


@router.get("/etapas")
def listar_etapas() -> "Dict[str, Any]":
    """A cascata canonica (para a tela montar o funil passo a passo)."""
    return {"etapas": ETAPAS_SEQUENCIA}


@router.post("/acoes/etapa/{etapa}")
def rodar_etapa(etapa: str, pedido: "Dict[str, Any]") -> "Dict[str, Any]":
    """Roda UMA etapa avulsa, fora da esteira (modo 'passo a passo')."""
    spec = _ETAPAS_POR_ID.get(etapa)
    if spec is None:
        raise HTTPException(status_code=422, detail=f"etapa desconhecida: {etapa!r}")
    subprojeto = str(pedido.get("subprojeto") or "")
    cmd = _cmd_etapa(etapa, subprojeto, pedido)
    job = motor.enfileirar(
        f"{spec['titulo']}" + (f" - {subprojeto}" if subprojeto else ""),
        cmd,
        classe=spec["classe"],
        cwd=str(RAIZ),
        meta={"acao": etapa, "subprojeto": subprojeto, "avulsa": True},
    )
    return {"job_id": job.id, "estado": job.estado, "classe": spec["classe"]}


@router.post("/acoes/sequencia")
def rodar_sequencia(pedido: PedidoSequencia) -> "Dict[str, Any]":
    """Enfileira a esteira: cada etapa so comeca quando a anterior CONCLUIR.

    Nada bloqueia o servidor: sao jobs encadeados por `depende_de`. Se uma etapa
    falhar, as seguintes sao canceladas -- rodar MD sobre um HADDOCK que quebrou
    so geraria numero com aparencia de resultado.
    """
    if not pedido.etapas:
        raise HTTPException(status_code=422, detail="nenhuma etapa escolhida")
    ordem = [e["id"] for e in ETAPAS_SEQUENCIA]
    desconhecidas = [e for e in pedido.etapas if e not in ordem]
    if desconhecidas:
        raise HTTPException(
            status_code=422, detail=f"etapas desconhecidas: {desconhecidas}"
        )
    # respeita SEMPRE a ordem da cascata, mesmo que a tela mande fora de ordem
    escolhidas = [e for e in ordem if e in pedido.etapas]

    criados: "List[Dict[str, Any]]" = []
    anterior: "str | None" = None
    for etapa in escolhidas:
        spec = _ETAPAS_POR_ID[etapa]
        p = dict(pedido.parametros.get(etapa) or {})
        cmd = _cmd_etapa(etapa, pedido.subprojeto, p)
        job = motor.enfileirar(
            f"{spec['titulo']} - {pedido.subprojeto}",
            cmd,
            classe=spec["classe"],
            cwd=str(RAIZ),
            meta={
                "acao": etapa,
                "subprojeto": pedido.subprojeto,
                "sequencia": escolhidas,
                "posicao": f"{len(criados) + 1}/{len(escolhidas)}",
            },
            depende_de=anterior,
        )
        anterior = job.id
        criados.append({"etapa": etapa, "job_id": job.id, "classe": spec["classe"]})
    return {"jobs": criados, "n": len(criados)}


class PedidoYaml(BaseModel):
    nome: str
    destino: str
    entidades: "List[Dict[str, str]]"


@router.post("/predicao/yaml")
def gerar_yaml(pedido: PedidoYaml) -> "Dict[str, Any]":
    """Resolve as cadeias e escreve o YAML de entrada (rapido, sem fila)."""
    from web import predicao

    try:
        return predicao.montar_yaml(pedido.nome, pedido.entidades, pedido.destino)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


class PedidoRelatorio(BaseModel):
    runs: "List[str]"
    saida: str
    projeto: str = ""
    figuras: bool = True
    html: bool = True
    pdf: bool = False


@router.post("/acoes/relatorio")
def rodar_relatorio(pedido: PedidoRelatorio) -> "Dict[str, Any]":
    """Enfileira a geracao do relatorio (`report` do CLI externo) das predicoes escolhidas."""
    if not pedido.runs:
        raise HTTPException(status_code=422, detail="nenhuma predicao escolhida")
    cmd = [sys.executable, "-m", "web.runner", "relatorio", "--saida", pedido.saida]
    if pedido.projeto:
        cmd += ["--projeto", pedido.projeto]
    if pedido.figuras:
        cmd.append("--figuras")
    if pedido.html:
        cmd.append("--html")
    if pedido.pdf:
        cmd.append("--pdf")
    cmd += ["--"] + list(pedido.runs)
    job = motor.enfileirar(
        f"relatorio ({len(pedido.runs)} predicao(oes))",
        cmd,
        classe="leve",
        cwd=str(RAIZ),
        meta={"acao": "relatorio", "n_runs": len(pedido.runs)},
    )
    return {"job_id": job.id, "estado": job.estado}
