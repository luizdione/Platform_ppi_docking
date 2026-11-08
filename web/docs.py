# -*- coding: utf-8 -*-
"""Documentos de apoio da aba Ajuda (Markdown -> HTML) e busca full-text.

Procura `.md` em `<repositorio>/docs` e nas pastas extras declaradas na env
`PPI_DOCS_DIRS` (separada por `os.pathsep`). Nenhuma documentacao acompanha
este repositorio por padrao: a aba fica vazia, dizendo onde por os arquivos.
Aponte a env para os seus manuais, ou crie a pasta `docs/`.

A ordem da lista e curada por `_TITULOS`; o resto entra por titulo.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List

#: pastas onde procurar os `.md`, na ordem de prioridade. So a do proprio
#: repositorio: um default apontando para outro repositorio seria um caminho
#: que quem instala o pacote nao tem. Use PPI_DOCS_DIRS para as suas.
_PADRAO = [
    Path(__file__).resolve().parent.parent / "docs",
]

#: titulo legivel por arquivo; o que nao estiver aqui entra com o nome do arquivo
_TITULOS = {
    "manual_ppi_insilico.md": "Manual didatico - PPI in silico",
    "tutorial_alphafold3_multimer_metricas_confianca.md": (
        "Tutorial AlphaFold3/Multimer - metricas de confianca"
    ),
    "metricas_confianca.md": "Metricas de confianca (ipTM, ipSAE, PAE)",
    "validacao_estrutural.md": "Validacao estrutural",
    "dinamica_molecular.md": "Dinamica molecular (MD)",
    "energia_livre.md": "Energia livre (MM/GBSA)",
    "receitas_experimento.md": "Receitas de experimento",
    "guia_uso.md": "Guia de uso da plataforma",
    "guia_publicacao.md": "Guia de publicacao",
    "glossario.md": "Glossario",
    "faq.md": "Perguntas frequentes",
}

#: os dois manuais longos abrem a lista
_PRIMEIROS = [
    "manual_ppi_insilico.md",
    "tutorial_alphafold3_multimer_metricas_confianca.md",
]

_ID_SEGURO = re.compile(r"^[A-Za-z0-9_.-]+$")


def pastas() -> "List[Path]":
    extras = os.environ.get("PPI_DOCS_DIRS", "")
    lista = [Path(p) for p in extras.split(os.pathsep) if p.strip()]
    return [p for p in (lista + _PADRAO) if p.is_dir()]


def listar() -> "List[Dict[str, Any]]":
    """Todos os `.md` visiveis, sem repetir (a 1a pasta que tiver o nome ganha)."""
    vistos: "Dict[str, Path]" = {}
    for pasta in pastas():
        for arquivo in sorted(pasta.glob("*.md")):
            vistos.setdefault(arquivo.name, arquivo)

    def ordem(nome: str) -> "tuple":
        if nome in _PRIMEIROS:
            return (0, _PRIMEIROS.index(nome))
        return (1, _TITULOS.get(nome, nome).lower())

    return [
        {
            "id": nome,
            "titulo": _TITULOS.get(nome, nome.replace("_", " ").replace(".md", "")),
            "caminho": str(caminho),
            "kb": round(caminho.stat().st_size / 1024),
        }
        for nome, caminho in sorted(vistos.items(), key=lambda kv: ordem(kv[0]))
    ]


def _resolver(doc_id: str) -> "Path | None":
    """Acha o arquivo do documento. O id NUNCA vira caminho (sem `..`, sem `/`)."""
    if not _ID_SEGURO.match(doc_id or "") or not doc_id.endswith(".md"):
        return None
    for pasta in pastas():
        alvo = pasta / doc_id
        if alvo.is_file():
            return alvo
    return None


def obter(doc_id: str) -> "Dict[str, Any] | None":
    """Documento renderizado em HTML, pronto para a pagina."""
    caminho = _resolver(doc_id)
    if caminho is None:
        return None
    texto = caminho.read_text(encoding="utf-8", errors="replace")
    try:
        import markdown

        html = markdown.markdown(
            texto, extensions=["tables", "fenced_code", "toc", "sane_lists"]
        )
    except Exception:  # noqa: BLE001 - sem a lib, mostra o texto cru (nao quebra)
        html = "<pre>" + texto.replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
    return {
        "id": doc_id,
        "titulo": _TITULOS.get(doc_id, doc_id),
        "caminho": str(caminho),
        "html": html,
    }


def buscar(consulta: str, max_hits: int = 40) -> "List[Dict[str, Any]]":
    """Busca full-text simples; devolve um trecho ao redor do 1o acerto."""
    termo = (consulta or "").strip().lower()
    if len(termo) < 2:
        return []
    achados: "List[Dict[str, Any]]" = []
    for item in listar():
        try:
            texto = Path(item["caminho"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        pos = texto.lower().find(termo)
        if pos < 0:
            continue
        ini = max(0, pos - 90)
        trecho = texto[ini : pos + 160].replace("\n", " ").strip()
        achados.append(
            {
                "id": item["id"],
                "titulo": item["titulo"],
                "trecho": ("..." if ini else "") + trecho + "...",
                "ocorrencias": texto.lower().count(termo),
            }
        )
        if len(achados) >= max_hits:
            break
    achados.sort(key=lambda a: a["ocorrencias"], reverse=True)
    return achados
