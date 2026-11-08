# -*- coding: utf-8 -*-
"""Painel FastAPI (`web/`): rotas, cache de manifesto e rotulo do par.

Sem GPU, sem rede e sem pontuar nada de verdade: a pontuacao real e cara (dezenas
de runs por subprojeto) e ja tem cobertura em `test_core_data.py`. Aqui o alvo e
o que o painel acrescenta -- o contrato das rotas, a maquina de estados do cache
e a divisao do rotulo do par.
"""

from __future__ import annotations

import json

import pytest

from core.data import split_pair_id

fastapi_testclient = pytest.importorskip("fastapi.testclient")


# --------------------------------------------------------------------------
# 1. rotulo do par (isca x parceiro)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "rotulo, esperado",
    [
        ("barnase_x_barstar", ("barnase", "barstar")),
        ("barnaseH102A_x_barstarC82A_ATP", ("barnaseH102A", "barstarC82A")),
        ("iscaecto__x__parceiroecto", ("iscaecto", "parceiroecto")),
        ("barnase_x_barstarW44F", ("barnase", "barstarW44F")),
        ("sem_separador", ("sem_separador", "")),
    ],
)
def test_split_pair_id(rotulo, esperado) -> None:
    """O rotulo do par e dividido em isca x parceiro, descartando cofator."""
    assert split_pair_id(rotulo) == esperado


def test_split_pair_id_descarta_sufixo_de_cofator() -> None:
    """`_ATP` e sufixo de cofator, nao parte do nome do parceiro."""
    _, parceiro = split_pair_id("barnase_x_barstarD39A")
    assert parceiro == "barstarD39A"
    assert not parceiro.endswith("_ATP")


# --------------------------------------------------------------------------
# 2. cache do manifesto (maquina de estados)
# --------------------------------------------------------------------------
@pytest.fixture()
def cache_isolado(tmp_path, monkeypatch):
    """Cache num diretorio temporario, com a memoria zerada entre testes."""
    monkeypatch.setenv("PPI_DOCK_CACHE_DIR", str(tmp_path))
    from web import cache as _cache

    _cache._MEM.clear()
    _cache._EM_CURSO.clear()
    yield _cache
    _cache._MEM.clear()
    _cache._EM_CURSO.clear()


def test_cache_ausente_quando_nao_ha_nada(cache_isolado) -> None:
    assert cache_isolado.pronto("SUBX", "impressao-1") is None
    assert cache_isolado.estado("SUBX", "impressao-1")["estado"] == cache_isolado.AUSENTE


def test_cache_le_do_disco_e_promove_para_memoria(cache_isolado) -> None:
    """Manifesto ja em disco tem de ser servido sem recalcular (o ponto do cache)."""
    manifesto = {"key": "SUBX", "titulo": "X", "pairs": [{"label": "a_x_b"}]}
    alvo = cache_isolado._arquivo("SUBX", "impressao-1")
    alvo.write_text(json.dumps(manifesto), encoding="utf-8")

    lido = cache_isolado.pronto("SUBX", "impressao-1")
    assert lido == manifesto
    assert cache_isolado.estado("SUBX", "impressao-1")["estado"] == cache_isolado.PRONTO
    # e agora tambem em memoria (2a leitura nem toca o disco)
    alvo.unlink()
    assert cache_isolado.pronto("SUBX", "impressao-1") == manifesto


def test_cache_impressao_diferente_nao_serve_dado_velho(cache_isolado) -> None:
    """Dado novo em disco (fingerprint diferente) invalida o cache sozinho."""
    manifesto = {"key": "SUBX", "pairs": []}
    cache_isolado._arquivo("SUBX", "impressao-1").write_text(
        json.dumps(manifesto), encoding="utf-8"
    )
    assert cache_isolado.pronto("SUBX", "impressao-1") == manifesto
    assert cache_isolado.pronto("SUBX", "impressao-2") is None


def test_cache_sem_impressao_nunca_cacheia(cache_isolado) -> None:
    """Sem fingerprint nao da para saber se envelheceu -> nao serve cache."""
    assert cache_isolado.pronto("SUBX", None) is None
    assert cache_isolado.garantir("SUBX", None)["estado"] == cache_isolado.AUSENTE


def test_cache_corrompido_e_descartado(cache_isolado) -> None:
    """JSON quebrado nao pode derrubar a pagina: some e refaz."""
    alvo = cache_isolado._arquivo("SUBX", "impressao-1")
    alvo.write_text("{isso nao e json", encoding="utf-8")
    assert cache_isolado.pronto("SUBX", "impressao-1") is None
    assert not alvo.exists()


# --------------------------------------------------------------------------
# 3. rotas do painel
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cliente():
    """App de verdade, mas SEM o aquecimento de cache.

    Na subida a app poe os subprojetos para pontuar em segundo plano; num teste
    isso dispararia subprocessos de pontuacao real (minutos, dado de verdade em
    E:). `PPI_WEB_NO_WARMUP=1` desliga exatamente isso.
    """
    import os

    from fastapi.testclient import TestClient

    anterior = os.environ.get("PPI_WEB_NO_WARMUP")
    os.environ["PPI_WEB_NO_WARMUP"] = "1"
    try:
        from web.app import app

        with TestClient(app) as c:
            yield c
    finally:
        if anterior is None:
            os.environ.pop("PPI_WEB_NO_WARMUP", None)
        else:
            os.environ["PPI_WEB_NO_WARMUP"] = anterior


def test_health_responde(cliente) -> None:
    r = cliente.get("/health")
    assert r.status_code == 200
    corpo = r.json()
    assert corpo["ok"] is True
    assert corpo["porta"] == 8777
    assert "toolchain" in corpo and "assets" in corpo


def test_painel_abre(cliente) -> None:
    """A landing tem de abrir mesmo que a config de subprojetos esteja torta."""
    r = cliente.get("/")
    assert r.status_code == 200
    assert "Plataforma PPI" in r.text


def test_css_compartilhado_e_servido(cliente) -> None:
    """O painel serve o ppi.css, fonte unica de estilo da interface."""
    r = cliente.get("/static/ppi.css")
    assert r.status_code == 200
    assert "--cy:#58a6ff" in r.text  # variavel da paleta do viewer


def test_subprojeto_desconhecido_erra_alto(cliente) -> None:
    """Chave inexistente responde 404 no contrato {"error": ...}, nunca 200 vazio."""
    r = cliente.get("/sub/NAO_EXISTE")
    assert r.status_code == 404
    assert "error" in r.json()


def test_estrutura_de_subprojeto_desconhecido_erra_alto(cliente) -> None:
    r = cliente.get("/estrutura/NAO_EXISTE/qualquer.cif")
    assert r.status_code == 404
    assert "error" in r.json()


def test_api_da_ponte_montada(cliente) -> None:
    """A ponte antiga (8778) foi absorvida em /api -- tem de responder aqui."""
    r = cliente.get("/api/health")
    assert r.status_code == 200
    assert r.json().get("ok") is True


# --------------------------------------------------------------------------
# 4. as abas do menu existem de verdade (e o erro nao vira JSON na cara)
# --------------------------------------------------------------------------
_COMO_NAVEGADOR = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}


@pytest.mark.parametrize("rota", ["/", "/jobs", "/ajuda"])
def test_toda_aba_do_menu_abre(cliente, rota) -> None:
    """Guarda contra link morto no menu.

    A aba Ajuda chegou a existir so no menu, sem rota: quem clicava recebia
    `{"error":"Not Found"}` na cara. Este teste quebra se alguem puser um
    link no `base.html` sem a pagina correspondente.
    """
    r = cliente.get(rota, headers=_COMO_NAVEGADOR)
    assert r.status_code == 200, f"{rota} respondeu {r.status_code}"
    assert "<!DOCTYPE html>" in r.text


def test_menu_nao_tem_link_morto(cliente) -> None:
    """Extrai os hrefs do menu e confirma que TODOS respondem."""
    import re

    pagina = cliente.get("/", headers=_COMO_NAVEGADOR).text
    menu = re.search(r'<nav class="navtop">(.*?)</nav>', pagina, re.S)
    assert menu, "o menu sumiu do base.html"
    destinos = re.findall(r'href="([^"]+)"', menu.group(1))
    assert destinos, "menu sem links"
    for destino in destinos:
        resposta = cliente.get(destino, headers=_COMO_NAVEGADOR)
        assert resposta.status_code == 200, f"link morto no menu: {destino}"


def test_nenhuma_pagina_tem_link_ou_asset_morto(cliente) -> None:
    """Varre TODO link/asset interno das paginas, nao so o menu.

    O teste acima cobre a barra de navegacao. O que quebrou na pratica, porem,
    foram links DENTRO do conteudo (um "ver jobs", um documento da Ajuda) e
    arquivos de `static/` renomeados: nada disso aparece no `<nav>`, e o usuario
    so descobre clicando. Aqui cada `href`/`src` interno de cada pagina e
    visitado uma vez.
    """
    import re

    paginas = [
        "/", "/novo", "/executar", "/jobs", "/relatorio", "/experimentos",
        "/diagnostico", "/ajuda",
    ]
    alvo = re.compile(r'(?:href|src)="(/[^"#/][^"]*)"')  # so interno, sem //cdn
    destinos: "dict[str, set]" = {}
    for pagina in paginas:
        resposta = cliente.get(pagina, headers=_COMO_NAVEGADOR)
        assert resposta.status_code == 200, f"{pagina} respondeu {resposta.status_code}"
        for achado in alvo.findall(resposta.text):
            destinos.setdefault(achado, set()).add(pagina)

    assert len(destinos) >= len(paginas), "varredura nao achou nem os links do menu"
    mortos = []
    for destino, origens in sorted(destinos.items()):
        codigo = cliente.get(destino, headers=_COMO_NAVEGADOR).status_code
        if codigo not in (200, 302, 303):
            mortos.append(f"{destino} -> {codigo} (citado em {', '.join(sorted(origens))})")
    assert not mortos, "link/asset morto:\n  " + "\n  ".join(mortos)


def test_erro_no_navegador_e_pagina_html(cliente) -> None:
    """Quem navega recebe PAGINA de erro, nao o JSON da API."""
    r = cliente.get("/nao-existe", headers=_COMO_NAVEGADOR)
    assert r.status_code == 404
    assert "<!DOCTYPE html>" in r.text
    assert "Painel" in r.text  # tem como voltar


def test_erro_na_api_continua_json(cliente) -> None:
    """A API nao pode receber HTML no lugar do contrato {"error": ...}."""
    r = cliente.get("/api/jobs/nao-existe")
    assert r.status_code == 404
    assert "error" in r.json()


def test_ajuda_lista_documentos(cliente) -> None:
    r = cliente.get("/ajuda", headers=_COMO_NAVEGADOR)
    assert r.status_code == 200
    assert "Documentos" in r.text or "Nenhum documento" in r.text


def test_ajuda_documento_inexistente_erra_alto(cliente) -> None:
    """Id de documento nao pode virar caminho (nem achar arquivo fora da pasta)."""
    r = cliente.get("/ajuda?doc=../../../etc/passwd", headers=_COMO_NAVEGADOR)
    assert r.status_code == 404


def test_ajuda_busca_responde(cliente) -> None:
    r = cliente.get("/ajuda?q=ipsae", headers=_COMO_NAVEGADOR)
    assert r.status_code == 200
    assert "Busca por" in r.text


# --------------------------------------------------------------------------
# 4. Nova predicao: o nome vira nome de ARQUIVO, entao e conferido
# --------------------------------------------------------------------------
@pytest.mark.parametrize("nome", ["", "   ", "a/b", "..\fora", "com espaco", "..", "."])
def test_nome_de_predicao_invalido_erra_alto(tmp_path, nome) -> None:
    """Nome torto tem de falhar AQUI, nao virar arquivo estranho em disco.

    O nome vai direto para `<destino>/<nome>.yaml`. Barra e contrabarra a
    whitelist ja barrava; o que passava era o nome so de pontos, que gerava um
    `...yaml` que ninguem reconhece depois.
    """
    from web.predicao import montar_yaml

    with pytest.raises(ValueError):
        montar_yaml(
            nome,
            [{"cadeia": "A", "tipo": "protein", "fonte": "sequencia", "valor": "MKT"}],
            str(tmp_path),
        )
    assert list(tmp_path.iterdir()) == [], "nao pode ter escrito nada"


def test_nome_de_predicao_valido_escreve_o_yaml(tmp_path) -> None:
    """Caminho feliz: gera o YAML do Boltz-2 com as duas cadeias, sem rede."""
    from web.predicao import montar_yaml

    saida = montar_yaml(
        "teste_par-1",
        [
            {"cadeia": "A", "tipo": "protein", "fonte": "sequencia", "valor": "MKTAYIAK"},
            {"cadeia": "B", "tipo": "protein", "fonte": "sequencia", "valor": "MGSSHHH"},
        ],
        str(tmp_path),
    )
    alvo = tmp_path / "teste_par-1.yaml"
    assert alvo.is_file()
    texto = alvo.read_text(encoding="utf-8")
    assert texto.startswith("version: 1")
    assert texto.count("- protein:") == 2
    assert saida["yaml"] == str(alvo)
