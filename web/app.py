# -*- coding: utf-8 -*-
"""App FastAPI do dashboard (porta 8777): interface + API numa so aplicacao.

A interface reusa `web/static/ppi.css` e `web/static/viewer3d.js`, que sao a
fonte unica de estilo e de visualizacao 3D do painel.

Contrato de erro (herdado da ponte que esta app absorve): toda falha responde
`{"error": "..."}` com 4xx/5xx, nunca um 200 plausivel.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from web import PORTA_PADRAO, __version__
from web.assets import ensure_assets

RAIZ = Path(__file__).resolve().parent
STATIC_DIR = RAIZ / "static"
TEMPLATES_DIR = RAIZ / "templates"

#: 3Dmol/plotly nao sao versionados: recria a partir do bundle do viewer e do
#: pacote `plotly` na subida. O relatorio aparece no /health.
ASSETS = ensure_assets()

@asynccontextmanager
async def _ciclo_de_vida(_: FastAPI):
    """Na subida, poe os subprojetos para pontuar em segundo plano.

    Desligavel por `PPI_WEB_NO_WARMUP=1` -- os testes usam isso para nao sair
    disparando subprocessos de pontuacao real (que levam minutos).
    """
    from web import jobs as motor

    motor.iniciar_motor()  # carrega o historico e sobe o escalonador da fila
    if os.environ.get("PPI_WEB_NO_WARMUP") != "1":
        _aquecer_cache()
    yield


app = FastAPI(title="PPI - painel", version=__version__, lifespan=_ciclo_de_vida)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _num(valor: Any, casas: int = 3) -> str:
    """Formata numero para a tabela; ausente vira 'n.d.' (nunca 0,0 fingido)."""
    if valor is None:
        return "n.d."
    try:
        return f"{float(valor):.{casas}f}"
    except (TypeError, ValueError):
        return str(valor)


templates.env.filters["num"] = _num

#: Rotas NOVAS do painel (fila de jobs, acoes, recursos). Entram ANTES do mount
#: da ponte de proposito: um `mount("/api", ...)` captura TODO `/api/*`, entao o
#: que for registrado depois dele nunca seria alcancado.
from web.api import autoteste as _api_autoteste  # noqa: E402
from web.api import jobs as _api_jobs  # noqa: E402

app.include_router(_api_jobs.router, prefix="/api", tags=["jobs"])
app.include_router(_api_autoteste.router, prefix="/api", tags=["autoteste"])

#: A antiga ponte (FastAPI que vivia sozinha na 8778) entra AQUI como sub-app em
#: `/api`, em vez de ser copiada: `/api/health`, `/api/score`, `/api/gate`,
#: `/api/haddock/prepare|run`, `/api/funnel/run`, `/api/status/{job_id}`,
#: `/api/jobs`. Um processo so, uma porta so -- e zero duplicacao de rota. Os
#: proprios `exception_handler` da sub-app continuam valendo dentro de `/api`.
#: As rotas de execucao (funil, HADDOCK3, MD/MM-GBSA) vivem em `web/api/`,
#: junto com a fila e as travas de recurso.
from bridge.app import app as _ponte  # noqa: E402  (import tardio de proposito)

app.mount("/api", _ponte)


# ---------------------------------------------------------------------------
# erros: sempre {"error": ...}, nunca 200 plausivel
# ---------------------------------------------------------------------------
_TITULO_ERRO = {
    404: "Pagina nao encontrada",
    403: "Sem permissao",
    409: "Conflito",
    422: "Pedido invalido",
    500: "Erro interno",
}


def _quer_html(request: Request) -> bool:
    """O cliente e um navegador navegando (e nao a API sendo consumida)?

    Sem isso, quem clica num link quebrado recebe `{"error": "Not Found"}` na
    cara -- que foi exatamente o que aconteceu com a aba Ajuda antes de ela
    ter rota. API continua recebendo JSON.
    """
    if request.url.path.startswith("/api/"):
        return False
    return "text/html" in (request.headers.get("accept") or "")


def _resposta_erro(
    request: Request, codigo: int, mensagem: str, detalhe: str = ""
):
    if _quer_html(request):
        return templates.TemplateResponse(
            request,
            "erro.html",
            {
                "codigo": codigo,
                "titulo": _TITULO_ERRO.get(codigo, "Erro"),
                "mensagem": mensagem,
                "detalhe": detalhe,
                "versao": __version__,
            },
            status_code=codigo,
        )
    return JSONResponse({"error": mensagem}, status_code=codigo)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    return _resposta_erro(request, exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _erro_validacao(request: Request, exc: RequestValidationError):
    return _resposta_erro(request, 422, f"payload invalido: {exc.errors()}")


@app.exception_handler(Exception)
async def _erro_interno(request: Request, exc: Exception):
    import traceback

    return _resposta_erro(
        request,
        500,
        f"{type(exc).__name__}: {exc}",
        detalhe=traceback.format_exc()[-2000:],
    )


# ---------------------------------------------------------------------------
# subprojetos + cache do que e caro
# ---------------------------------------------------------------------------
def _subs():
    from core import data as core

    return core.load_subprojects()


def _subprojetos() -> "List[Dict[str, Any]]":
    """Subprojetos navegaveis, ja em forma de dict para o template.

    Nunca levanta: se a configuracao estiver quebrada, devolve lista vazia e a
    pagina mostra o aviso -- o painel tem de abrir mesmo com config torta.
    """
    try:
        subs = _subs()
    except Exception:  # noqa: BLE001 - a pagina mostra o estado vazio
        return []
    return [
        {
            "key": s.key,
            "name": s.name,
            "campaign": s.campaign,
            "n_roots": len(s.results_roots),
            "notes": s.notes or "",
        }
        for s in subs
    ]


def _sub_por_key(key: str):
    for s in _subs():
        if s.key == key:
            return s
    raise HTTPException(status_code=404, detail=f"subprojeto desconhecido: {key!r}")


def _impressao(sub) -> "str | None":
    from core import data as core

    try:
        return core.fingerprint_runs(sub)
    except Exception:  # noqa: BLE001 - sem fingerprint nao ha como cachear
        return None


def _manifesto_pronto(sub) -> "Dict[str, Any] | None":
    """Manifesto se ja estiver pronto; senao ``None`` (NUNCA calcula na requisicao)."""
    from web import cache

    return cache.pronto(sub.key, _impressao(sub))


def _pedir_manifesto(sub) -> "Dict[str, Any]":
    """Poe o subprojeto na fila de pontuacao (sem bloquear) e devolve o estado."""
    from web import cache

    return cache.garantir(sub.key, _impressao(sub))


def _aquecer_cache() -> None:
    """Pontua os subprojetos em segundo plano, na subida da app.

    SEQUENCIAL de proposito: pontuar tudo de uma vez satura a CPU e deixa o
    servidor irresponsivo (aconteceu). Aqui vai um por vez, esperando cada um
    terminar, e a `cache.garantir` garante que um acesso do usuario no meio do
    caminho aproveite a mesma thread em vez de disparar outra.
    """

    def _tarefa() -> None:
        try:
            subs = _subs()
        except Exception:  # noqa: BLE001
            return
        from web import cache

        for s in subs:
            try:
                _pedir_manifesto(s)
                while cache.estado(s.key, _impressao(s)).get("estado") == cache.CALCULANDO:
                    time.sleep(2.0)
            except Exception:  # noqa: BLE001 - aquecer e best-effort
                continue

    threading.Thread(target=_tarefa, name="aquece-cache", daemon=True).start()


#: manifesto -> colunas que `core.graficos_confianca` espera
#: (as mesmas de `TABELA_INDICES_COLUNAS`; nome errado aqui vira grafico vazio).
_COLUNAS_GRAFICO = {
    "classe": "classe",
    "iptm": "ipTM",
    "iface_iptm": "iface_ipTM",
    "ipsae": "ipSAE",
    "ptm": "pTM",
    "confidence_score": "conf",
    "complex_plddt": "pLDDT",
    "pae_iface_mean": "PAE_iface",
    "bsa": "BSA",
    "n_contacts": "n_contatos",
}


def _graficos(pares: "List[Dict[str, Any]]") -> "tuple[Dict[str, str], str]":
    """Figuras Plotly (JSON) para o cliente desenhar.

    Devolve ``(figuras, erro)``. O erro NAO e engolido: sobe para a pagina, que
    o mostra -- grafico que some sozinho esconde bug, e este projeto trata
    degradacao silenciosa como defeito.
    """
    validos = [p for p in pares if not p.get("erro")]
    if not validos:
        return {}, ""
    try:
        import pandas as pd

        from core import data as core

        linhas = []
        for p in validos:
            linha: "Dict[str, Any]" = {"par": f"{p['isca']} x {p['parceiro']}"}
            for origem, destino in _COLUNAS_GRAFICO.items():
                linha[destino] = p.get(origem)
            linhas.append(linha)
        figs = core.graficos_confianca(pd.DataFrame(linhas))
        return {nome: fig.to_json() for nome, fig in figs.items()}, ""
    except Exception as exc:  # noqa: BLE001 - a tabela segue valendo, mas avisa
        return {}, f"grafico indisponivel ({type(exc).__name__}: {exc})"


# ---------------------------------------------------------------------------
# rotas
# ---------------------------------------------------------------------------
@app.get("/health")
def health() -> "Dict[str, Any]":
    """Sanidade da app: versao, porta, assets e estado da toolchain do HADDOCK3."""
    try:
        from core import data as core

        toolchain = core.haddock3_toolchain_status()
    except Exception as exc:  # noqa: BLE001 - health nunca deve derrubar
        toolchain = {"available": False, "erro": f"{type(exc).__name__}: {exc}"}
    return {
        "ok": True,
        "versao": __version__,
        "porta": PORTA_PADRAO,
        "assets": ASSETS,
        "toolchain": toolchain,
    }


@app.get("/")
def painel(request: Request):
    """Painel inicial: um card por subprojeto (mesmo layout da landing do viewer)."""
    return templates.TemplateResponse(
        request,
        "painel.html",
        {"subprojetos": _subprojetos(), "versao": __version__},
    )


@app.get("/estado/{key}")
def estado_subprojeto(key: str) -> "Dict[str, Any]":
    """Situacao da pontuacao de um subprojeto (a pagina "pontuando..." consulta)."""
    from web import cache

    sub = _sub_por_key(key)
    return cache.estado(sub.key, _impressao(sub))


@app.get("/sub/{key}")
def subprojeto(request: Request, key: str):
    """Pagina de um subprojeto: indices, graficos e estrutura 3D.

    Se o manifesto ainda nao estiver pronto, a pagina NAO espera: pede o calculo
    e devolve a tela de "pontuando...", que se atualiza sozinha. Pontuar o DEMO01
    leva ~7 min -- prender a requisicao nisso trava o servidor inteiro.
    """
    sub = _sub_por_key(key)
    manifesto = _manifesto_pronto(sub)
    if manifesto is None:
        situacao = _pedir_manifesto(sub)
        return templates.TemplateResponse(
            request,
            "pontuando.html",
            {"sub": sub, "situacao": situacao, "versao": __version__},
        )
    erro = ""
    pares = manifesto.get("pairs", [])
    contagem = {"HIT": 0, "MAYBE": 0, "weak": 0}
    for p in pares:
        if not p.get("erro"):
            contagem[p["classe"]] = contagem.get(p["classe"], 0) + 1
    figuras, erro_grafico = _graficos(pares)
    return templates.TemplateResponse(
        request,
        "subprojeto.html",
        {
            "sub": sub,
            "pares": pares,
            "contagem": contagem,
            "n_erros": sum(1 for p in pares if p.get("erro")),
            "pairs_json": json.dumps(manifesto, ensure_ascii=False),
            "charts_json": json.dumps(figuras, ensure_ascii=False),
            "erro_grafico": erro_grafico,
            "erro": erro,
            "versao": __version__,
        },
    )


@app.get("/jobs")
def pagina_jobs(request: Request):
    """Fila e historico de jobs, com log ao vivo e estado dos recursos."""
    from web import jobs as motor

    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": motor.listar(50),
            "recursos": motor.recursos(),
            "versao": __version__,
        },
    )


@app.get("/executar")
def pagina_executar(request: Request):
    """Funil passo a passo: a cascata inteira encadeada, ou uma etapa avulsa."""
    from web.api.jobs import ETAPAS_SEQUENCIA

    return templates.TemplateResponse(
        request,
        "executar.html",
        {
            "subprojetos": _subprojetos(),
            "etapas": ETAPAS_SEQUENCIA,
            "versao": __version__,
        },
    )


@app.get("/diagnostico")
def pagina_diagnostico(request: Request):
    """Sanidade do ambiente: dependencias, ferramentas, pastas e GPU."""
    from web import motor_ppi

    ponte = motor_ppi.disponivel()
    saude: "Dict[str, Any]" = {}
    erro = ""
    if ponte.get("ok"):
        try:
            saude = motor_ppi.diagnostico()
        except Exception as exc:  # noqa: BLE001 - a pagina explica
            erro = f"{type(exc).__name__}: {exc}"
    return templates.TemplateResponse(
        request,
        "diagnostico.html",
        {
            "ponte": ponte,
            "saude": saude,
            "erro": erro,
            "recursos": __import__("web.jobs", fromlist=["jobs"]).recursos(),
            "autoteste": _api_autoteste.estado(),
            "versao": __version__,
        },
    )


@app.get("/novo")
def pagina_nova_predicao(request: Request):
    """Nova predicao: monta o complexo e gera o YAML de entrada do Boltz-2."""
    from web import predicao

    return templates.TemplateResponse(
        request,
        "novo.html",
        {
            "subprojetos": _subprojetos(),
            "tipos": predicao.TIPOS,
            "fontes": predicao.FONTES,
            "versao": __version__,
        },
    )


@app.get("/relatorio")
def pagina_relatorio(request: Request, key: str = ""):
    """Relatorio das predicoes de um subprojeto (Markdown/HTML/PDF)."""
    from web import motor_ppi

    subs = _subprojetos()
    escolhido = key or (subs[0]["key"] if subs else "")
    runs: "List[Dict[str, Any]]" = []
    erro = ""
    if escolhido:
        try:
            sub = _sub_por_key(escolhido)
            runs = motor_ppi.runs_de(list(sub.results_roots))
        except Exception as exc:  # noqa: BLE001
            erro = f"{type(exc).__name__}: {exc}"
    return templates.TemplateResponse(
        request,
        "relatorio.html",
        {
            "subprojetos": subs,
            "key": escolhido,
            "runs": runs,
            "erro": erro,
            "versao": __version__,
        },
    )


@app.get("/experimentos")
def pagina_experimentos(request: Request):
    """Receitas de experimento declaradas no projeto-alvo."""
    from web import motor_ppi

    ponte = motor_ppi.disponivel()
    dados: "Dict[str, Any]" = {"alvo": "", "receitas": []}
    erro = ""
    if ponte.get("ok"):
        try:
            dados = motor_ppi.receitas()
        except Exception as exc:  # noqa: BLE001
            erro = f"{type(exc).__name__}: {exc}"
    return templates.TemplateResponse(
        request,
        "experimentos.html",
        {"ponte": ponte, "dados": dados, "erro": erro, "versao": __version__},
    )


@app.get("/ajuda")
def pagina_ajuda(request: Request, doc: str = "", q: str = ""):
    """Manuais e tutoriais, com busca full-text. Os `.md` vivem no repo do motor."""
    from web import docs as _docs

    documentos = _docs.listar()
    escolhido = _docs.obter(doc) if doc else None
    if doc and escolhido is None:
        return _resposta_erro(request, 404, f"documento desconhecido: {doc!r}")
    return templates.TemplateResponse(
        request,
        "ajuda.html",
        {
            "documentos": documentos,
            "doc": escolhido,
            "consulta": q,
            "achados": _docs.buscar(q) if q else [],
            "versao": __version__,
        },
    )


@app.get("/estrutura/{key}/{label}.cif")
def estrutura(key: str, label: str):
    """Serve o `.cif` de um par para o viewer 3D.

    O `label` NAO vira caminho: e casado contra os pares realmente descobertos
    em disco, entao nao ha como escapar da raiz do subprojeto.
    """
    from core import data as core

    sub = _sub_por_key(key)
    for par in core.discover_pairs(sub):
        if par.pair_id == label:
            if not par.cif_model:
                raise HTTPException(
                    status_code=404, detail=f"par {label!r} sem .cif em disco"
                )
            return FileResponse(par.cif_model, media_type="chemical/x-cif")
    raise HTTPException(status_code=404, detail=f"par desconhecido: {label!r}")
