# -*- coding: utf-8 -*-
"""Nucleo de dados da plataforma, SEM nenhum framework de interface.

Os DOIS consumidores dependem deste modulo: o painel (`web/`, FastAPI) e o
gerador do viewer estatico (`tools/build_viewer.py`). Por isso ele nao pode
arrastar dependencia de UI -- ha um teste que importa este arquivo com a UI
bloqueada, justamente para impedir que volte.

Responsabilidades:

* ler as campanhas declaradas em ``config/subproject.*.yaml``;
* descobrir os pares (runs do Boltz-2 ja rodados) de uma campanha em disco;
* juntar scores de planilha + manifesto do funil numa TABELA pronta de exibir;
* montar o manifesto dos pares no contrato do viewer 3D.

Regras herdadas do projeto:

* **Nunca degradar em silencio.** Diretorio ausente, coluna faltando ou binario
  inexistente viram erro/aviso EXPLICITO. Celula vazia vira ``None``, jamais
  ``0.0``.
* **Nenhuma logica de gate aqui.** O veredicto vem do `PairManifest` gravado
  pelo funil. Se o par ainda nao foi avaliado, a tabela diz "nao avaliado" -
  aqui nao se recalcula portao nenhum (seria codigo duplicado e, pior, uma
  segunda verdade).
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from funnel.contract import PairManifest, load_manifest

__all__ = [
    "GuiError",
    "Subproject",
    "PairRef",
    "REPO_ROOT",
    "CONFIG_DIR",
    "CURATED_SUBPROJECTS_ENV",
    "load_subprojects",
    "curated_subprojects_path",
    "chain_roles_for",
    "load_funnel_config",
    "discover_pairs",
    "observe_pairs",
    "OBSERVE_FIELDS",
    "tabela_indices",
    "TABELA_INDICES_COLUNAS",
    "fingerprint_runs",
    "graficos_confianca",
    "structure_view_html",
    "load_scores_tsv",
    "load_manifests",
    "manifests_from_response",
    "build_funnel_table",
    "TABLE_COLUMNS",
    "approved_rows",
    "read_text_artifact",
    "haddock3_toolchain_status",
    "split_pair_id",
    "pairs_manifest",
]

# Raiz do repo = pasta acima de core/.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent
CONFIG_DIR: Path = REPO_ROOT / "config"

# JSON CURADO com os subprojetos: um indice de navegacao OPCIONAL que aponta
# para arvores de resultados Boltz-2 ja existentes. E DADO, nao codigo - mora
# FORA do repositorio, no disco de quem usa a plataforma. Esta camada so guarda
# um PONTEIRO para ele; nunca copia os dados apontados.
# Resolucao: (1) argumento explicito; (2) env `PPI_SUBPROJECTS`. NAO existe
# default de disco - um caminho fixo aqui apontaria para a maquina de quem
# escreveu o codigo, nunca para a de quem o usa. Sem nenhum dos dois, a camada
# cai nos `config/subproject.*.yaml` versionados.
CURATED_SUBPROJECTS_ENV = "PPI_SUBPROJECTS"


class GuiError(RuntimeError):
    """Erro de configuracao/descoberta da GUI (mensagem ja legivel ao usuario)."""


# --------------------------------------------------------------------------
# helpers internos
# --------------------------------------------------------------------------
def _as_float_or_none(texto: "Any | None") -> "float | None":
    """Converte celula de planilha para float; vazio/'None'/'nan' vira None."""
    if texto is None:
        return None
    if isinstance(texto, (int, float)) and not isinstance(texto, bool):
        return float(texto)
    s = str(texto).strip()
    if s == "" or s.lower() in {"none", "nan", "na", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        # Nao inventa 0.0: o valor existe mas nao e numerico -> erro claro.
        raise GuiError(f"valor nao numerico onde se esperava numero: {texto!r}")


def _as_int_or_none(texto: "Any | None") -> "int | None":
    """Idem para contagens inteiras."""
    valor = _as_float_or_none(texto)
    return None if valor is None else int(valor)


def _as_bool_or_none(texto: "Any | None") -> "bool | None":
    """Interpreta a coluna de veredicto manual ('True'/'False'/vazio)."""
    if texto is None:
        return None
    if isinstance(texto, bool):
        return texto
    s = str(texto).strip().lower()
    if s in {"true", "1", "sim", "yes", "aprovado"}:
        return True
    if s in {"false", "0", "nao", "não", "no", "reprovado"}:
        return False
    return None


def _read_yaml(path: Path) -> "Dict[str, Any]":
    """Le um YAML de configuracao. Ausencia/malformacao = erro claro."""
    try:
        import yaml  # dependencia declarada em pyproject
    except ImportError as exc:  # pragma: no cover - ambiente sem pyyaml
        raise GuiError(
            "pyyaml nao esta instalado no ambiente ativo "
            "(pip install pyyaml)."
        ) from exc
    if not path.is_file():
        raise GuiError(f"arquivo de configuracao ausente: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GuiError(f"YAML invalido em {path}: {exc}") from exc
    if data is None:
        raise GuiError(f"YAML vazio: {path}")
    if not isinstance(data, dict):
        raise GuiError(f"YAML de {path} deveria ser um mapeamento, veio {type(data).__name__}")
    return data


# --------------------------------------------------------------------------
# Campanha (subprojeto) declarada em config/subproject.*.yaml
# --------------------------------------------------------------------------
@dataclass
class Subproject:
    """Uma campanha do funil (ex.: DEMO01 barnase), lida de um YAML de config.

    * ``results_roots`` - pastas onde estao os runs do Boltz-2 ja rodados;
    * ``run_glob`` - padrao das pastas de run (ex.: ``boltz_results_*``);
    * ``run_prefix`` - prefixo a remover do nome da pasta para obter o ``pair_id``;
    * ``scores_tsv`` / ``scores_columns`` - planilha de scores ja calculada e o
      mapeamento coluna->campo (mantido em config para nao hard-codar o formato
      de um projeto dentro do codigo);
    * ``manifests_dir`` - onde o funil grava os ``PairManifest`` (fonte do gate);
    * ``work_dir`` - raiz de trabalho do HADDOCK3 desta campanha (fora do repo).
    """

    key: str
    name: str
    campaign: str
    results_roots: "List[str]" = field(default_factory=list)
    run_glob: str = "boltz_results_*"
    run_prefix: str = "boltz_results_"
    model_suffix: str = "_model_0.cif"
    scores_tsv: "str | None" = None
    scores_columns: "Dict[str, str]" = field(default_factory=dict)
    manual_flag_column: "str | None" = None
    manifests_dir: "str | None" = None
    work_dir: "str | None" = None
    label_prefixes: "List[str]" = field(default_factory=list)
    # Ponteiros de navegacao vindos do subprojetos.json curado (fonte da verdade
    # da navegacao, vindos do indice curado). Ausentes nos YAMLs
    # locais (ficam None). NUNCA sao usados para copiar dado - so para localizar.
    input_root: "str | None" = None
    out_root: "str | None" = None
    source_yaml: "str | None" = None
    notes: "str | None" = None
    raw: "Dict[str, Any]" = field(default_factory=dict)

    @property
    def chain_roles(self) -> "Dict[str, Any]":
        """Bloco ``chain_roles`` do YAML (papeis explicitos das cadeias)."""
        bloco = self.raw.get("chain_roles")
        return dict(bloco) if isinstance(bloco, Mapping) else {}

    @classmethod
    def from_dict(cls, data: "Mapping[str, Any]", source: "Path | None" = None) -> "Subproject":
        """Constroi a partir do dict do YAML, exigindo os campos de identidade.

        Aceita os DOIS dialetos de ``config/subproject.*.yaml``:

        * **aninhado** (o versionado): ``id`` / ``label`` / ``paths.results_root``
          / ``paths.avaliacoes`` / ``paths.haddock_out`` / ``scores.tsv``;
        * **plano**: ``key`` / ``name`` / ``campaign`` / ``results_roots`` etc.

        A normalizacao mora em ``_normalizar_subprojeto``.
        """
        bruto = dict(data)
        data = _normalizar_subprojeto(bruto, source)
        for obrigatorio in ("key", "name", "campaign", "results_roots"):
            if not data.get(obrigatorio):
                raise GuiError(
                    f"config de subprojeto sem campo obrigatorio '{obrigatorio}'"
                    + (f" ({source})" if source else "")
                )
        roots = data["results_roots"]
        if isinstance(roots, str):
            roots = [roots]
        return cls(
            key=str(data["key"]),
            name=str(data["name"]),
            campaign=str(data["campaign"]),
            results_roots=[str(r) for r in roots],
            run_glob=str(data.get("run_glob") or "boltz_results_*"),
            run_prefix=str(data.get("run_prefix") or ""),
            model_suffix=str(data.get("model_suffix") or "_model_0.cif"),
            scores_tsv=(str(data["scores_tsv"]) if data.get("scores_tsv") else None),
            scores_columns={str(k): str(v) for k, v in (data.get("scores_columns") or {}).items()},
            manual_flag_column=(
                str(data["manual_flag_column"]) if data.get("manual_flag_column") else None
            ),
            manifests_dir=(str(data["manifests_dir"]) if data.get("manifests_dir") else None),
            work_dir=(str(data["work_dir"]) if data.get("work_dir") else None),
            label_prefixes=[str(p) for p in (data.get("label_prefixes") or [])],
            source_yaml=(str(source) if source else None),
            notes=(str(data["notes"]) if data.get("notes") else None),
            raw=bruto,
        )


# Mapeamento default coluna->campo para uma planilha LONG de scores (usado
# quando o YAML da campanha nao declara `scores.columns`). Colunas declaradas
# mas ausentes da planilha viram erro claro em load_scores_tsv.
_COLUNAS_SCORES_LONG: "Dict[str, str]" = {
    "label": "label",
    "ipsae": "ipSAE",
    "iptm": "ipTMpp",          # protein_iptm (sem ligantes), nao o iptm global
    "coverage_core_face": "cov_core",
    "face": "face",
    "n_iface_res": "iface",
}


def _normalizar_subprojeto(
    data: "Mapping[str, Any]", source: "Path | None" = None
) -> "Dict[str, Any]":
    """Traduz o dialeto ANINHADO do YAML de campanha para o dialeto plano.

    O YAML versionado (``subproject.<nome>.yaml``) descreve a campanha com
    ``id``/``label``/``paths``/``scores``; o dataclass trabalha com um dict
    plano. Caminho declarado mas vazio = erro claro, nunca um default inventado.
    """
    if "paths" not in data:
        return dict(data)
    paths = data.get("paths")
    if not isinstance(paths, Mapping):
        raise GuiError(
            f"'paths' deveria ser um mapeamento em {source or '(config)'}, "
            f"veio {type(paths).__name__}"
        )
    roots = paths.get("results_roots") or paths.get("results_root")
    if not roots:
        raise GuiError(f"campanha sem 'paths.results_root' em {source or '(config)'}")
    if isinstance(roots, str):
        roots = [roots]

    scores = data.get("scores") if isinstance(data.get("scores"), Mapping) else {}
    tsv = scores.get("tsv")
    scores_tsv = None
    if tsv:
        avaliacoes = paths.get("avaliacoes")
        # nome relativo precisa da pasta de avaliacoes; caminho absoluto passa direto
        p = Path(str(tsv))
        if p.is_absolute():
            scores_tsv = str(p)
        elif avaliacoes:
            scores_tsv = str(Path(str(avaliacoes)) / str(tsv))
        else:
            raise GuiError(
                f"'scores.tsv' e relativo ({tsv!r}) mas 'paths.avaliacoes' nao "
                f"foi declarado em {source or '(config)'}"
            )

    haddock_out = paths.get("haddock_out")
    manifests = paths.get("manifests") or (
        str(Path(str(haddock_out)) / "manifests") if haddock_out else None
    )
    identificador = str(data.get("id") or data.get("key") or "").strip()
    if not identificador:
        raise GuiError(f"campanha sem 'id' em {source or '(config)'}")
    rotulo = str(data.get("label") or data.get("name") or identificador).strip()

    colunas = scores.get("columns")
    return {
        "key": identificador,
        "name": rotulo,
        "campaign": str(data.get("campaign") or identificador),
        "results_roots": [str(r) for r in roots],
        "run_glob": data.get("run_glob") or "boltz_results_*",
        "run_prefix": data.get("run_prefix", "boltz_results_"),
        "model_suffix": data.get("model_suffix") or "_model_0.cif",
        "scores_tsv": scores_tsv,
        "scores_columns": dict(colunas) if isinstance(colunas, Mapping) else dict(_COLUNAS_SCORES_LONG),
        "manual_flag_column": scores.get("manual_column", "aprovado"),
        "manifests_dir": manifests,
        "work_dir": haddock_out,
        "label_prefixes": data.get("label_prefixes") or [],
        "notes": data.get("description") or data.get("notes"),
    }


def _load_local_subprojects(config_dir: "str | Path | None" = None) -> "List[Subproject]":
    """Le os ``config/subproject.*.yaml`` versionados (dialeto rico com
    ``chain_roles``/``scores``). Pasta ausente = erro claro; pasta SEM YAML =
    lista vazia (nao e mais erro: o subprojetos.json curado pode suprir as
    campanhas). Quem exige "ao menos uma campanha" e o ``load_subprojects``."""
    pasta = Path(config_dir) if config_dir else CONFIG_DIR
    if not pasta.is_dir():
        raise GuiError(f"pasta de configuracao inexistente: {pasta}")
    arquivos = sorted(pasta.glob("subproject.*.yaml")) + sorted(pasta.glob("subproject.*.yml"))
    return [Subproject.from_dict(_read_yaml(a), a) for a in arquivos]


def curated_subprojects_path(
    explicit: "str | Path | None" = None,
) -> "Path | None":
    """Resolve o caminho do ``subprojetos.json`` curado, sem le-lo.

    Ordem: (1) ``explicit``; (2) env ``PPI_SUBPROJECTS``. NAO ha terceiro nivel:
    sem argumento e sem env o resultado e ``None``, e a camada de dados cai nos
    ``config/subproject.*.yaml`` versionados. Um caminho
    apontado mas inexistente NAO vira erro aqui - vira ``None`` (ausencia). Um
    JSON existente porem malformado, sim, quebra alto (ver ``_read_curated``)."""
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    env = os.environ.get(CURATED_SUBPROJECTS_ENV, "").strip()
    if env and Path(env).is_file():
        return Path(env)
    return None


def _read_curated(path: Path) -> "Dict[str, Any]":
    """Le o subprojetos.json curado. JSON invalido/malformado = erro claro
    (nunca cair em silencio para os YAMLs quando o arquivo EXISTE porem esta
    quebrado - isso esconderia um subprojeto do usuario)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise GuiError(f"subprojetos.json invalido em {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("subprojetos"), list):
        raise GuiError(
            f"subprojetos.json malformado (esperava objeto com a lista "
            f"'subprojetos'): {path}"
        )
    return data


def _subproject_from_curated(item: "Mapping[str, Any]", source: Path) -> Subproject:
    """Constroi um ``Subproject`` a partir de UM item do subprojetos.json curado
    (``{id, label, roots:[...], input_root?, out_root?}``). Guarda so PONTEIROS
    aos dados no disco do usuario - nunca os copia. ``run_prefix``/``run_glob``/
    ``model_suffix`` ficam nos defaults do Boltz-2 (``boltz_results_*`` +
    ``_model_0.cif``), que e o layout real desses runs."""
    ident = str(item.get("id") or "").strip()
    if not ident:
        raise GuiError(f"item de subprojetos.json sem 'id' ({source})")
    roots = [str(r) for r in (item.get("roots") or []) if r]
    if not roots:
        raise GuiError(f"subprojeto {ident!r} sem 'roots' em {source}")
    return Subproject(
        key=ident,
        name=str(item.get("label") or ident),
        campaign=ident,
        results_roots=roots,
        input_root=(str(item["input_root"]) if item.get("input_root") else None),
        out_root=(str(item["out_root"]) if item.get("out_root") else roots[0]),
        source_yaml=None,
        raw=dict(item),
    )


def _merge_local_into_curated(base: Subproject, local: Subproject) -> Subproject:
    """Funde o item CURADO (``base``, fonte da navegacao: roots/input_root) com o
    YAML LOCAL (``local``, que COMPLEMENTA: ``chain_roles``, ``scores_tsv``,
    ``scores_columns``, prefixos de run etc.). Sem duplicar: um unico
    ``Subproject`` por id. Os ``roots`` vem do JSON curado (navegacao); todo o
    resto rico vem do YAML. ``chain_roles`` sobrevive porque mora em
    ``raw`` (o bruto do YAML)."""
    return Subproject(
        key=base.key,
        name=base.name or local.name,
        campaign=local.campaign or base.campaign,
        results_roots=base.results_roots or local.results_roots,
        run_glob=local.run_glob,
        run_prefix=local.run_prefix,
        model_suffix=local.model_suffix,
        scores_tsv=local.scores_tsv,
        scores_columns=dict(local.scores_columns),
        manual_flag_column=local.manual_flag_column,
        manifests_dir=local.manifests_dir,
        work_dir=local.work_dir,
        label_prefixes=list(local.label_prefixes),
        input_root=base.input_root or local.input_root,
        out_root=base.out_root or local.out_root,
        source_yaml=local.source_yaml,
        notes=local.notes or base.notes,
        raw=dict(local.raw),  # <- chain_roles vem daqui (bruto do YAML)
    )


def load_subprojects(
    config_dir: "str | Path | None" = None,
    *,
    subprojects_json: "str | Path | None" = None,
) -> "List[Subproject]":
    """Todos os subprojetos que a GUI enxerga, ordenados por ``key``.

    Duas fontes, fundidas por ``id``:

    * o **subprojetos.json curado** (env ``PPI_SUBPROJECTS``) - indice de
      navegacao OPCIONAL, apontando para arvores de resultados Boltz-2 que ja
      existem no disco de quem usa. Guarda so ponteiros, nunca dados.
    * os ``config/subproject.*.yaml`` locais - dialeto rico com ``chain_roles`` e
      mapeamento de colunas de score. Quando um id existe nos dois, o YAML
      **complementa** o item do JSON (nao duplica).

    Se o subprojetos.json faltar, cai para os YAMLs locais (comportamento antigo)
    sem quebrar. Se AMBAS as fontes estiverem vazias, ai sim erra alto - a GUI
    nao inventa uma campanha default apontando para disco arbitrario.
    """
    locais = _load_local_subprojects(config_dir)
    por_id: "Dict[str, Subproject]" = {s.key: s for s in locais}

    curados: "Dict[str, Subproject]" = {}
    json_path = curated_subprojects_path(subprojects_json)
    if json_path is not None:
        for item in _read_curated(json_path).get("subprojetos", []):
            if not isinstance(item, Mapping):
                raise GuiError(
                    f"item de subprojetos.json nao e um objeto ({type(item).__name__}) "
                    f"em {json_path}"
                )
            sub = _subproject_from_curated(item, json_path)
            curados[sub.key] = sub

    if not curados and not por_id:
        pasta = Path(config_dir) if config_dir else CONFIG_DIR
        raise GuiError(
            f"nenhuma campanha declarada: nem 'subproject.*.yaml' em {pasta} nem "
            f"subprojetos.json curado (defina a env {CURATED_SUBPROJECTS_ENV} com o "
            "caminho do indice, ou declare a campanha num YAML versionado)."
        )

    resultado: "Dict[str, Subproject]" = {}
    # JSON curado e a base (fonte da navegacao); YAML local complementa.
    for ident, sub_json in curados.items():
        local = por_id.get(ident)
        resultado[ident] = (
            _merge_local_into_curated(sub_json, local) if local is not None else sub_json
        )
    # YAML local sem correspondente no JSON entra como esta (nada regride se E: sumir).
    for ident, local in por_id.items():
        resultado.setdefault(ident, local)

    return sorted(resultado.values(), key=lambda s: s.key)


def chain_roles_for(
    identificador: str, config_dir: "str | Path | None" = None
) -> "Dict[str, Any]":
    """Bloco ``chain_roles`` DECLARADO pela campanha (por ``key`` ou ``campaign``).

    E a fonte unica dos papeis de cadeia para todo o guarda-chuva: quem prepara
    o HADDOCK3 (GUI, ponte, n8n) passa este bloco adiante, e os papeis param de
    depender da ORDEM das cadeias no `.cif` - que no DEMO01 esta invertida
    (A = barstar, B = barnase).

    Campanha inexistente = ``GuiError`` com a lista das conhecidas (a ponte
    responderia 422 em vez de preparar o par com o papel trocado). Campanha sem
    o bloco = ``{}``, e ai o chamador decide se cai no fallback posicional.
    """
    alvo = str(identificador).strip()
    subs = load_subprojects(config_dir)
    for s in subs:
        if alvo in {s.key, s.campaign, s.name}:
            return s.chain_roles
    conhecidas = ", ".join(f"{s.key}/{s.campaign}" for s in subs)
    raise GuiError(
        f"campanha {alvo!r} nao declarada em config/subproject.*.yaml "
        f"(conhecidas: {conhecidas})"
    )


def load_funnel_config(path: "str | Path | None" = None) -> "Dict[str, Any]":
    """Le ``config/funnel.yaml`` (limiares dos portoes) - usado pela aba Ajuda."""
    return _read_yaml(Path(path) if path else CONFIG_DIR / "funnel.yaml")


# --------------------------------------------------------------------------
# Descoberta dos pares em disco
# --------------------------------------------------------------------------
@dataclass
class PairRef:
    """Um par (run do Boltz-2) encontrado em disco.

    ``problems`` guarda o que faltou (predicoes ausentes, .cif faltando...).
    Um par com problema continua aparecendo na tabela, marcado - some da lista
    seria exatamente o tipo de degradacao silenciosa que o projeto proibe.
    """

    pair_id: str
    run_dir: str
    cif_model: "str | None" = None
    confidence_json: "str | None" = None
    pae_npz: "str | None" = None
    problems: "List[str]" = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True se o par tem os arquivos minimos para seguir no funil."""
        return not self.problems


def discover_pairs(sub: Subproject) -> "List[PairRef]":
    """Lista os runs do Boltz-2 de uma campanha (ordenados por ``pair_id``).

    Layout esperado (verificado no piloto DEMO01)::

        <root>/<run_glob>/predictions/<pair_id>/<pair_id>_model_0.cif
                                               confidence_<pair_id>_model_0.json
                                               pae_<pair_id>_model_0.npz

    Raiz inexistente levanta ``GuiError`` - nao devolve lista vazia fingindo
    que a campanha nao tem pares.
    """
    if not isinstance(sub, Subproject):
        raise TypeError(f"discover_pairs: esperava Subproject, veio {type(sub).__name__}")
    pares: "List[PairRef]" = []
    vistos: "set[str]" = set()  # dedup GLOBAL por caminho (roots podem se sobrepor)
    for root_txt in sub.results_roots:
        root = Path(root_txt)
        if not root.is_dir():
            raise GuiError(f"raiz de resultados inexistente: {root}")
        # Mesma varredura do dashboard_core.discover_runs_in: runs no TOPO da raiz
        # ou dentro de uma subpasta de lote (`<root>/<lote>/boltz_results_*`).
        run_dirs: "List[Path]" = []
        for pat in (sub.run_glob, f"*/{sub.run_glob}"):
            for p in sorted(root.glob(pat)):
                if p.is_dir() and str(p) not in vistos:
                    vistos.add(str(p))
                    run_dirs.append(p)
        for run_dir in run_dirs:
            nome = run_dir.name
            pair_id = nome[len(sub.run_prefix):] if (
                sub.run_prefix and nome.startswith(sub.run_prefix)
            ) else nome
            if sub.label_prefixes and not any(pair_id.startswith(p) for p in sub.label_prefixes):
                continue
            pares.append(_inspect_run(run_dir, pair_id, sub.model_suffix))
    pares.sort(key=lambda p: p.pair_id)
    return pares


def _inspect_run(run_dir: Path, pair_id: str, model_suffix: str) -> PairRef:
    """Confere os arquivos de UM run e devolve o ``PairRef`` correspondente."""
    ref = PairRef(pair_id=pair_id, run_dir=str(run_dir))
    pred = run_dir / "predictions" / pair_id
    if not pred.is_dir():
        # tolera um unico subdiretorio com nome diferente do rotulo
        candidatos = (
            sorted(p for p in (run_dir / "predictions").glob("*") if p.is_dir())
            if (run_dir / "predictions").is_dir()
            else []
        )
        if len(candidatos) == 1:
            pred = candidatos[0]
        else:
            ref.problems.append("pasta 'predictions/<label>' ausente ou ambigua")
            return ref
    cif = pred / f"{pred.name}{model_suffix}"
    if cif.is_file():
        ref.cif_model = str(cif)
    else:
        ref.problems.append(f"modelo ausente: {cif.name}")
    conf = pred / f"confidence_{pred.name}{model_suffix.replace('.cif', '.json')}"
    if conf.is_file():
        ref.confidence_json = str(conf)
    else:
        ref.problems.append(f"confidence ausente: {conf.name}")
    pae = pred / f"pae_{pred.name}{model_suffix.replace('.cif', '.npz')}"
    if pae.is_file():
        ref.pae_npz = str(pae)
    else:
        ref.problems.append(f"PAE ausente: {pae.name}")
    return ref


# --------------------------------------------------------------------------
# Indices de confianca por par (calculados AO VIVO com o motor scoring_engine.score)
# --------------------------------------------------------------------------
# Campos numericos de cada linha de observe_pairs (fora pair_id/run_dir/cif/
# classe/erro). Espelham os atributos do `scoring_engine.score.Score`.
OBSERVE_FIELDS: "tuple[str, ...]" = (
    "iptm",
    "iface_iptm",
    "ipsae",
    "ptm",
    "confidence_score",
    "complex_plddt",
    "pae_iface_mean",
    "iface_plddt",
    "bsa",
    "n_contacts",
    "n_iface_res",
)


def _linha_observacao_vazia(par: PairRef, erro: str) -> "Dict[str, Any]":
    """Linha de um par SEM como pontuar (cif/confidence ausente, score falhou).

    Todos os indices vem None (nunca 0.0) e a chave ``erro`` diz o porque - o par
    NAO some da tabela (some seria a degradacao silenciosa que o projeto proibe).
    """
    linha: "Dict[str, Any]" = {
        "pair_id": par.pair_id,
        "run_dir": par.run_dir,
        "cif": par.cif_model,
        "classe": None,
        "erro": erro,
    }
    for campo in OBSERVE_FIELDS:
        linha[campo] = None
    return linha


def observe_pairs(sub: Subproject, *, model: int = 0) -> "List[Dict[str, Any]]":
    """Calcula os indices de confianca de CADA par do subprojeto, ao vivo.

    Uma linha por run de ``discover_pairs`` (logo ``len(observe_pairs) ==
    len(discover_pairs)``). Reusa o motor ``scoring_engine.score`` (import preguicoso) sobre
    o ``model_0`` de cada run - ``score.score_case(run_dir)`` re-localiza o
    ``.cif``/``confidence``/``pae`` de forma recursiva, entao o layout
    ``predictions/<par>/`` destes runs e aceito como esta (validado contra o
    DEMO02 barnase). Cada dict traz::

        pair_id, run_dir, cif, iptm, iface_iptm, ipsae, ptm, confidence_score,
        complex_plddt, pae_iface_mean, iface_plddt, bsa, n_contacts,
        n_iface_res, classe (HIT/MAYBE/weak via scoring_engine.rank.classify)

    Regras (herdadas do projeto):

    * campo ausente -> ``None``, jamais ``0.0``;
    * run incompleto (sem ``.cif`` ou sem ``confidence``) ou score que falha ->
      linha COM a chave ``erro`` explicita, nunca pulada em silencio.

    ``model`` != 0 ainda nao e suportado: ``scoring_engine.score.score_case`` pontua o
    ``model_0``. Pedir outro modelo ERRA ALTO (nao pontua o 0 fingindo ser o N).
    """
    if not isinstance(sub, Subproject):
        raise TypeError(f"observe_pairs: esperava Subproject, veio {type(sub).__name__}")
    if int(model) != 0:
        raise GuiError(
            f"observe_pairs: apenas o model 0 e suportado por scoring_engine.score.score_case "
            f"(pedido: model={model})."
        )
    # Import preguicoso: este modulo continua importavel num ambiente sem
    # numpy/biopython (os dois sao exigidos pelo motor de pontuacao).
    try:
        from scoring_engine import rank as _rank
        from scoring_engine import score as _score
    except Exception as exc:  # noqa: BLE001
        raise GuiError(
            "motor de pontuacao indisponivel: 'scoring_engine' exige numpy e "
            f"biopython no ambiente ativo (pip install -e .). Detalhe: {exc}"
        ) from exc

    linhas: "List[Dict[str, Any]]" = []
    for par in discover_pairs(sub):
        # Sem .cif ou sem confidence nao ha o que pontuar: erro explicito.
        if not par.cif_model or not par.confidence_json:
            motivo = "; ".join(par.problems) or "cif/confidence ausente"
            linhas.append(_linha_observacao_vazia(par, motivo))
            continue
        try:
            sc = _score.score_case(par.run_dir)
        except Exception as exc:  # noqa: BLE001
            linhas.append(
                _linha_observacao_vazia(par, f"{exc.__class__.__name__}: {exc}")
            )
            continue
        if sc is None:
            linhas.append(
                _linha_observacao_vazia(
                    par, "scoring_engine.score.score_case retornou None (sem cif valido ou <2 cadeias)"
                )
            )
            continue
        linha: "Dict[str, Any]" = {
            "pair_id": par.pair_id,
            "run_dir": par.run_dir,
            "cif": par.cif_model,
            "classe": _rank.classify(sc),
            "erro": None,
        }
        for campo in OBSERVE_FIELDS:
            linha[campo] = getattr(sc, campo, None)
        linhas.append(linha)
    return linhas


# --------------------------------------------------------------------------
# Mirante de resultados: tabela de indices, graficos e viewer 3D
# --------------------------------------------------------------------------
# Colunas da tabela do mirante (chave interna JA legivel; o app so troca por
# rotulos bonitos se quiser). Ordem = ordem de exibicao. Espelham observe_pairs.
TABELA_INDICES_COLUNAS: "tuple[str, ...]" = (
    "par",
    "classe",
    "ipTM",
    "iface_ipTM",
    "ipSAE",
    "pTM",
    "conf",
    "pLDDT",
    "PAE_iface",
    "BSA",
    "n_contatos",
)

# De onde cada coluna do mirante sai em observe_pairs (chave_observe -> coluna).
_OBS_TO_TABELA: "Dict[str, str]" = {
    "pair_id": "par",
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

# Ordem das classes na tabela/graficos (HIT antes de MAYBE antes de weak; runs
# sem classe - os com 'erro' - vao para o fim).
_CLASSE_ORDEM: "Dict[str, int]" = {"HIT": 0, "MAYBE": 1, "weak": 2}

# Cores por classe (usadas nos graficos e no realce da tabela no app).
_CORES_CLASSE: "Dict[str, str]" = {
    "HIT": "#2ea043",
    "MAYBE": "#d29922",
    "weak": "#8b949e",
}

# Paleta ciclica para colorir cada cadeia no viewer 3D + cores do realce da
# interface (isca x parceiro).
_PALETA_CADEIA: "tuple[str, ...]" = (
    "#4c8bf5",
    "#f5a04c",
    "#59c78b",
    "#c75990",
    "#9b7ad6",
    "#d0c14a",
)
_COR_ISCA = "#1f6f43"
_COR_PARCEIRO = "#b3521f"


def fingerprint_runs(sub: Subproject) -> str:
    """Assinatura BARATA das runs de um subprojeto (para chave de cache).

    Sem pontuar nada: apenas conta as pastas de run e pega o mtime mais novo.
    Muda quando uma predicao e adicionada/refeita, entao o app pode cachear
    ``tabela_indices`` por ``(sub.key, fingerprint_runs(sub))`` e so re-scorar
    quando o disco realmente mudou (uma campanha de dezenas de runs nao
    re-scora a cada
    clique). Raiz inexistente e simplesmente ignorada aqui (quem erra alto e o
    ``discover_pairs``); assim o fingerprint nunca quebra a pagina.
    """
    if not isinstance(sub, Subproject):
        raise TypeError(f"fingerprint_runs: esperava Subproject, veio {type(sub).__name__}")
    latest = 0.0
    n = 0
    vistos: "set[str]" = set()
    for root_txt in sub.results_roots:
        root = Path(root_txt)
        if not root.is_dir():
            continue
        for pat in (sub.run_glob, f"*/{sub.run_glob}"):
            for p in root.glob(pat):
                if not p.is_dir() or str(p) in vistos:
                    continue
                vistos.add(str(p))
                n += 1
                try:
                    m = p.stat().st_mtime
                except OSError:
                    m = 0.0
                if m > latest:
                    latest = m
    return f"{n}:{latest:.0f}"


def tabela_indices(sub: Subproject, *, model: int = 0) -> "Any":
    """Tabela (``pandas.DataFrame``) dos indices de confianca por par.

    Reusa ``observe_pairs`` (que pontua ao vivo com ``scoring_engine.score``), projeta nas
    colunas ``TABELA_INDICES_COLUNAS`` (ja legiveis) e ordena HIT > MAYBE > weak
    e, dentro da classe, por ``iface_ipTM`` decrescente. Runs incompletos/que
    falharam ao pontuar NAO somem: aparecem no fim, com os indices ``None`` (nunca
    0.0) e o motivo na coluna ``erro`` - a mesma regra anti-degradacao-silenciosa
    do resto do projeto. Valores ausentes ficam ``None``/``NaN``, jamais 0.0.

    A coluna ``erro`` fica no fim da tabela (fora de ``TABELA_INDICES_COLUNAS``)
    para o app poder avisar sobre os pares problematicos sem polui-los como zero.
    """
    import pandas as pd  # dependencia opcional de analise

    linhas = observe_pairs(sub, model=model)
    registros: "List[Dict[str, Any]]" = []
    for l in linhas:
        reg = {rotulo: l.get(chave) for chave, rotulo in _OBS_TO_TABELA.items()}
        reg["erro"] = l.get("erro")
        registros.append(reg)

    colunas = list(TABELA_INDICES_COLUNAS) + ["erro"]
    df = pd.DataFrame(registros, columns=colunas)
    if df.empty:
        return df

    def _iface_key(valor: "Any") -> float:
        if valor is None or pd.isna(valor):
            return float("-inf")
        return float(valor)

    df["_rank"] = df["classe"].map(lambda c: _CLASSE_ORDEM.get(c, 3))
    df["_iface"] = df["iface_ipTM"].map(_iface_key)
    df = (
        df.sort_values(["_rank", "_iface"], ascending=[True, False])
        .drop(columns=["_rank", "_iface"])
        .reset_index(drop=True)
    )
    return df


def graficos_confianca(df: "Any") -> "Dict[str, Any]":
    """Figuras Plotly (``go.Figure``) dos indices de confianca. SEM I/O, SEM st.

    Recebe a tabela de ``tabela_indices`` e devolve um dict de figuras (o app so
    faz ``st.plotly_chart``). Cada figura e colorida por classe (HIT/MAYBE/weak).
    Linhas com o indice ausente (``None``/``NaN``) sao ignoradas naquela figura -
    nunca viram um ponto em 0.0. Chaves devolvidas::

        barras_iface_iptm - barras de iface-ipTM por par
        dispersao_iptm_pae - dispersao ipTM x PAE de interface
        barras_ipsae      - barras de ipSAE por par
    """
    import pandas as pd
    import plotly.graph_objects as go

    def _cor(classe: "str | None") -> str:
        return _CORES_CLASSE.get(classe, "#8b949e")

    def _validos(coluna: str) -> "Any":
        if df is None or getattr(df, "empty", True) or coluna not in df.columns:
            return df.iloc[0:0] if df is not None else None
        return df[df[coluna].map(lambda v: v is not None and not pd.isna(v))]

    classes = ("HIT", "MAYBE", "weak")

    # (1) barras de iface-ipTM por par, colorido por classe
    fig1 = go.Figure()
    base = _validos("iface_ipTM")
    if base is not None:
        for classe in classes:
            sub = base[base["classe"] == classe]
            if sub.empty:
                continue
            fig1.add_bar(
                x=sub["par"], y=sub["iface_ipTM"], name=classe, marker_color=_cor(classe)
            )
    fig1.update_layout(
        title="iface-ipTM por par (cor = classe)",
        xaxis_title="par",
        yaxis_title="iface-ipTM",
        barmode="group",
        legend_title="classe",
    )

    # (2) dispersao ipTM x PAE de interface
    fig2 = go.Figure()
    if df is not None and not getattr(df, "empty", True):
        d2 = df[
            df["ipTM"].map(lambda v: v is not None and not pd.isna(v))
            & df["PAE_iface"].map(lambda v: v is not None and not pd.isna(v))
        ]
        for classe in classes:
            sub = d2[d2["classe"] == classe]
            if sub.empty:
                continue
            fig2.add_scatter(
                x=sub["ipTM"],
                y=sub["PAE_iface"],
                mode="markers",
                name=classe,
                marker=dict(color=_cor(classe), size=9),
                text=sub["par"],
                hovertemplate=(
                    "%{text}<br>ipTM=%{x:.3f}<br>PAE_iface=%{y:.2f} A<extra></extra>"
                ),
            )
    fig2.update_layout(
        title="ipTM x PAE de interface (cor = classe)",
        xaxis_title="ipTM",
        yaxis_title="PAE de interface (A)",
        legend_title="classe",
    )

    # (3) barras de ipSAE por par
    fig3 = go.Figure()
    base3 = _validos("ipSAE")
    if base3 is not None:
        for classe in classes:
            sub = base3[base3["classe"] == classe]
            if sub.empty:
                continue
            fig3.add_bar(
                x=sub["par"], y=sub["ipSAE"], name=classe, marker_color=_cor(classe)
            )
    fig3.update_layout(
        title="ipSAE por par (cor = classe)",
        xaxis_title="par",
        yaxis_title="ipSAE",
        barmode="group",
        legend_title="classe",
    )

    return {
        "barras_iface_iptm": fig1,
        "dispersao_iptm_pae": fig2,
        "barras_ipsae": fig3,
    }


def _cadeias_para_colorir(
    iface: "Mapping[str, Any] | None", caminho: Path
) -> "List[str]":
    """IDs de cadeia para colorir no viewer 3D.

    Preferencia: reaproveitar o que ``funnel.interface.iface_residues`` ja
    descobriu (``chain_sizes`` + ``hetero_chains``) para NAO reparsear o .cif. Se
    o iface nao veio, cai para uma leitura leve via ``scoring_engine._common.parse_model``;
    se ate isso falhar, devolve ``[]`` (o viewer usa entao um esquema unico, sem
    quebrar).
    """
    if iface:
        sizes = iface.get("chain_sizes") or {}
        hetero = iface.get("hetero_chains") or []
        chs = list(sizes.keys()) + [c for c in hetero if c not in sizes]
        if chs:
            return [str(c) for c in chs]
    try:
        from scoring_engine import _common

        model = _common.parse_model(caminho)
        return [str(c.id) for c in model]
    except Exception:  # noqa: BLE001 - viewer nunca deve quebrar por causa da cor
        return []


def structure_view_html(
    cif_path: "str | Path",
    *,
    iface: "Mapping[str, Any] | None" = None,
    altura: int = 480,
    largura: int = 900,
) -> str:
    """HTML de um viewer py3Dmol do COMPLEXO (cartoon colorido por cadeia).

    Le o ``.cif`` como texto e monta um viewer 3D auto-contido (o ``_make_html``
    do py3Dmol embute o 3Dmol.js - sem CDN externo), pronto para o app injetar
    com ``st.components.v1.html``. Cada cadeia recebe uma cor; se ``iface`` (o
    dict de ``funnel.interface.iface_residues``, com ``bait_residues``/
    ``partner_residues``) for dado, os residuos de interface viram *sticks*
    coloridos - e isso que mostra a INTERACAO isca x parceiro.

    ``.cif`` inexistente levanta ``GuiError`` (nunca devolve um viewer vazio que
    pareceria "sem estrutura").
    """
    import py3Dmol

    caminho = Path(cif_path)
    if not caminho.is_file():
        raise GuiError(f"modelo .cif inexistente para o viewer 3D: {caminho}")
    texto = caminho.read_text(encoding="utf-8")

    view = py3Dmol.view(width=int(largura), height=int(altura))
    view.addModel(texto, "cif")

    cadeias = _cadeias_para_colorir(iface, caminho)
    if cadeias:
        for i, ch in enumerate(cadeias):
            view.setStyle(
                {"chain": ch},
                {"cartoon": {"color": _PALETA_CADEIA[i % len(_PALETA_CADEIA)]}},
            )
    else:
        # sem saber as cadeias, ao menos desenha o complexo (colorido por residuo)
        view.setStyle({}, {"cartoon": {"color": "spectrum"}})

    if iface:
        bait_chains = [str(c) for c in (iface.get("bait_chains") or [])]
        partner = iface.get("partner_chain")
        bait_res = [int(r) for r in (iface.get("bait_residues") or [])]
        partner_res = [int(r) for r in (iface.get("partner_residues") or [])]
        for ch in bait_chains:
            if bait_res:
                view.addStyle(
                    {"chain": ch, "resi": bait_res},
                    {"stick": {"radius": 0.35, "color": _COR_ISCA}},
                )
        if partner and partner_res:
            view.addStyle(
                {"chain": str(partner), "resi": partner_res},
                {"stick": {"radius": 0.35, "color": _COR_PARCEIRO}},
            )

    view.setBackgroundColor("white")
    view.zoomTo()
    return view._make_html()


# --------------------------------------------------------------------------
# Scores de planilha + manifestos do funil
# --------------------------------------------------------------------------
def load_scores_tsv(sub: Subproject) -> "Dict[str, Dict[str, Any]]":
    """Le a planilha de scores da campanha, indexada pelo rotulo do par.

    O mapeamento coluna->campo vem do YAML (``scores_columns``), com a chave
    ``label`` obrigatoria. Coluna declarada mas inexistente = erro claro
    (silenciar viraria uma tabela cheia de "-" sem explicacao). Devolve ``{}``
    se a campanha nao declarou planilha.
    """
    if not sub.scores_tsv:
        return {}
    caminho = Path(sub.scores_tsv)
    if not caminho.is_file():
        raise GuiError(f"planilha de scores inexistente: {caminho}")
    colunas = dict(sub.scores_columns)
    coluna_label = colunas.pop("label", None)
    if not coluna_label:
        raise GuiError(
            f"{sub.source_yaml or sub.key}: 'scores_columns' precisa mapear 'label'."
        )
    linhas = caminho.read_text(encoding="utf-8").splitlines()
    if not linhas:
        raise GuiError(f"planilha de scores vazia: {caminho}")
    cabecalho = linhas[0].split("\t")
    faltando = [c for c in [coluna_label, *colunas.values()] if c not in cabecalho]
    if sub.manual_flag_column and sub.manual_flag_column not in cabecalho:
        faltando.append(sub.manual_flag_column)
    if faltando:
        raise GuiError(
            f"colunas ausentes em {caminho.name}: {', '.join(sorted(set(faltando)))} "
            f"(cabecalho real: {', '.join(cabecalho)})"
        )
    idx = {nome: i for i, nome in enumerate(cabecalho)}
    numericos = {"ipsae", "iptm", "iface_iptm", "coverage_core_face", "pdockq", "complex_plddt"}
    inteiros = {"n_iface_res", "n_contacts"}
    saida: "Dict[str, Dict[str, Any]]" = {}
    for linha in linhas[1:]:
        if not linha.strip():
            continue
        celulas = linha.split("\t")
        def _cel(nome: str) -> "str | None":
            i = idx[nome]
            return celulas[i] if i < len(celulas) else None
        rotulo = (_cel(coluna_label) or "").strip()
        if not rotulo:
            continue
        registro: "Dict[str, Any]" = {}
        for campo, coluna in colunas.items():
            bruto = _cel(coluna)
            if campo in numericos:
                registro[campo] = _as_float_or_none(bruto)
            elif campo in inteiros:
                registro[campo] = _as_int_or_none(bruto)
            else:
                texto = (bruto or "").strip()
                registro[campo] = texto or None
        if sub.manual_flag_column:
            registro["manual"] = _as_bool_or_none(_cel(sub.manual_flag_column))
        saida[rotulo] = registro
    return saida


def load_manifests(sub: Subproject) -> "Dict[str, PairManifest]":
    """Le os ``PairManifest`` ja gravados pelo funil, indexados por ``pair_id``.

    Pasta ainda inexistente = ``{}`` (o funil simplesmente nao rodou); JSON
    corrompido, porem, propaga o erro do ``load_manifest``.
    """
    if not sub.manifests_dir:
        return {}
    pasta = Path(sub.manifests_dir)
    if not pasta.is_dir():
        return {}
    return {m.pair_id: m for m in (load_manifest(p) for p in sorted(pasta.glob("*.json")))}


# --------------------------------------------------------------------------
# Tabela do funil
# --------------------------------------------------------------------------
# Ordem das colunas exibidas (chave interna -> rotulo na GUI fica no app.py).
TABLE_COLUMNS: "tuple[str, ...]" = (
    "par",
    "gate",
    "ipSAE",
    "ipTM_pp",
    "cobertura_face_nuclear",
    "face",
    "n_iface",
    "manual",
    "estagio",
    "motivos",
    "problemas",
)

_GATE_NAO_AVALIADO = "nao avaliado"


def build_funnel_table(
    pairs: "Iterable[PairRef]",
    scores: "Mapping[str, Mapping[str, Any]] | None" = None,
    manifests: "Mapping[str, PairManifest] | None" = None,
) -> "List[Dict[str, Any]]":
    """Monta as linhas da tabela do funil, prontas para exibicao.

    Precedencia dos scores: **manifesto** (o que o funil realmente usou) tem
    prioridade sobre a **planilha** (avaliacao previa). Ausencia continua sendo
    ``None`` - a formatacao para "-" e responsabilidade da camada Streamlit.

    O rotulo do gate NUNCA e calculado aqui: vem do manifesto ou fica
    "nao avaliado".
    """
    scores = scores or {}
    manifests = manifests or {}
    linhas: "List[Dict[str, Any]]" = []
    for par in pairs:
        planilha = dict(scores.get(par.pair_id) or {})
        man = manifests.get(par.pair_id)
        s = man.scores if man else None

        def _pick(campo: str) -> "Any | None":
            """Valor do manifesto, se preenchido; senao o da planilha; senao None."""
            if s is not None:
                valor = getattr(s, campo, None)
                # contagens zeradas no manifesto significam "ainda nao contado"
                if valor is not None and not (campo in _CONTAGENS and valor == 0):
                    return valor
            return planilha.get(campo)

        if man is None:
            gate, motivos, estagio = _GATE_NAO_AVALIADO, [], "-"
        else:
            gate = "APROVADO" if man.gate_boltz.approved else "REPROVADO"
            motivos = list(man.gate_boltz.reasons)
            estagio = man.stage

        linhas.append(
            {
                "par": par.pair_id,
                "gate": gate,
                "aprovado": (man.gate_boltz.approved if man else None),
                "ipSAE": _pick("ipsae"),
                "ipTM_pp": _pick("iptm"),
                "cobertura_face_nuclear": _pick("coverage_core_face"),
                "face": _pick("face"),
                "n_iface": _pick("n_iface_res"),
                "manual": planilha.get("manual"),
                "estagio": estagio,
                "motivos": "; ".join(motivos),
                "problemas": "; ".join(par.problems),
                "run_dir": par.run_dir,
                "cif_model": (man.cif_model if man and man.cif_model else par.cif_model),
                "cfg": (man.haddock.cfg if man else None),
                "ambig_tbl": (man.haddock.ambig_tbl if man else None),
                "run_dir_haddock": (man.haddock.run_dir if man else None),
            }
        )
    return linhas


# Campos de contagem: 0 no manifesto = "ainda nao contado", nao um zero medido.
_CONTAGENS: "frozenset[str]" = frozenset({"n_iface_res", "n_contacts"})


def manifests_from_response(resposta: "Mapping[str, Any]") -> "Dict[str, PairManifest]":
    """Extrai os ``PairManifest`` devolvidos pela ponte, indexados por ``pair_id``.

    A ponte NAO grava manifesto em disco: ``POST /funnel/run`` sincrono devolve
    ``{aprovados: [...], reprovados: [...], erros: [...]}`` e
    ``POST /haddock/prepare`` devolve ``{manifest: {...}}``. Esta funcao converte
    isso no mesmo dicionario que ``load_manifests`` produz, para a tabela poder
    mostrar o veredicto recem-calculado.

    Manifesto malformado propaga o erro do contrato - nao vira linha silenciosa.
    """
    encontrados: "Dict[str, PairManifest]" = {}
    if not isinstance(resposta, Mapping):
        raise GuiError(
            f"resposta da ponte deveria ser um objeto JSON, veio {type(resposta).__name__}"
        )
    unico = resposta.get("manifest")
    if isinstance(unico, Mapping):
        m = PairManifest.from_dict(unico)
        encontrados[m.pair_id] = m
    for chave in ("aprovados", "reprovados"):
        for item in resposta.get(chave) or []:
            if isinstance(item, Mapping):
                m = PairManifest.from_dict(item)
                encontrados[m.pair_id] = m
    return encontrados


def approved_rows(rows: "Iterable[Mapping[str, Any]]") -> "List[Dict[str, Any]]":
    """Filtra apenas os pares APROVADOS no GATE 1 (os candidatos ao HADDOCK3)."""
    return [dict(r) for r in rows if r.get("aprovado") is True]


def read_text_artifact(path: "str | Path | None", max_bytes: int = 200_000) -> str:
    """Le um artefato de texto (.cfg / ambig.tbl / log) para preview.

    Caminho ausente ou arquivo inexistente levanta ``GuiError`` - a GUI mostra
    o aviso em vez de exibir um preview vazio que pareceria "AIR sem restricao".
    """
    if not path:
        raise GuiError("artefato ainda nao gerado (rode 'Preparar' antes).")
    p = Path(path)
    if not p.is_file():
        raise GuiError(f"artefato inexistente em disco: {p}")
    dados = p.read_bytes()[: max_bytes + 1]
    texto = dados[:max_bytes].decode("utf-8", errors="replace")
    if len(dados) > max_bytes:
        texto += f"\n... [truncado em {max_bytes} bytes]"
    return texto


# --------------------------------------------------------------------------
# Toolchain do HADDOCK3
# --------------------------------------------------------------------------
def haddock3_toolchain_status() -> "Dict[str, Any]":
    """Diz se o HADDOCK3 esta disponivel -- por binario LOCAL **ou** pela IMAGEM Docker.

    Motivo: o ``run()`` do adaptador usa motor "auto" -- binario local se existir,
    senao o container ``ppi-haddock3``. Esta funcao reflete essa MESMA
    disponibilidade, para a GUI nao dizer "nao instalado" quando o motor Docker
    esta pronto. No Windows o binario local nao existe (o CNS, motor de
    calculo do HADDOCK3, so tem build para Linux/macOS), entao o caminho normal e
    o Docker.

    Duas resolucoes, sem hard-code: ``funnel.haddock_adapter.check_toolchain()``
    (binario, a MESMA que a ponte usa) e ``funnel.haddock_docker.check_image()``
    (imagem). Devolve ``available = local OU docker`` mais o detalhe de cada via.
    Mantem as chaves que a GUI le (``available``/``via``/``detail``/``path``) e
    acrescenta ``local``/``docker``/``motor``. Nunca levanta.
    """
    local = {"available": False, "path": None}
    via = "funnel.haddock_adapter.check_toolchain"

    # --- binario local (via adaptador, respeita DOCKING_SUITE_HOME) ---
    try:
        from funnel import haddock_adapter

        info = haddock_adapter.check_toolchain()
        entrada = (info.get("tools") or {}).get("haddock3") or {}
        local = {
            "available": bool(info.get("available")),
            "path": info.get("path") or entrada.get("path"),
        }
    except Exception as exc:  # repo dos motores fora do lugar, import quebrado
        caminho = shutil.which("haddock3")
        local = {"available": bool(caminho), "path": caminho}
        via = "shutil.which (fallback)"

    # --- imagem Docker (o motor real onde nao ha binario) ---
    docker = {"available": False, "image": None, "error": None}
    try:
        from funnel import haddock_docker

        img = haddock_docker.check_image()
        docker = {
            "available": bool(img.get("available")),
            "image": img.get("image"),
            "error": img.get("error"),
            "motivo": img.get("motivo"),
        }
    except Exception as exc:
        docker = {"available": False, "image": None, "motivo": "erro",
                  "error": f"{exc.__class__.__name__}: {exc}"}

    disponivel = bool(local["available"] or docker["available"])

    if local["available"]:
        motor = "local"
        detail = f"haddock3 (binario local) em {local['path']}"
    elif docker["available"]:
        motor = "docker"
        via = "funnel.haddock_docker.check_image"
        detail = (
            f"haddock3 via Docker (imagem {docker['image']}). O binario local nao "
            "existe -- o CNS so tem build para Linux/macOS -- mas o motor roda no "
            "container; 'Rodar' usa a imagem."
        )
    elif docker.get("motivo") == "daemon_parado":
        # Nao mandar reconstruir: a imagem provavelmente existe e so o servico
        # esta fora do ar. Conselho errado aqui custa uma hora de build a toa.
        motor = "nenhum"
        detail = (
            "haddock3 parado porque o Docker nao esta no ar (Docker Desktop "
            "fechado?). Abra o Docker Desktop -- a imagem ppi-haddock3 nem "
            "precisa ser reconstruida. 'Preparar' funciona (logica pura); "
            "'Rodar' vai falhar ate o Docker subir."
        )
    else:
        motor = "nenhum"
        detail = (
            "haddock3 indisponivel: sem binario local (o CNS nao roda em Windows) e "
            f"sem a imagem Docker ({docker['error'] or 'imagem ausente'}). Construa com "
            "'docker build -f docker/Dockerfile.haddock3 -t ppi-haddock3:latest .'. "
            "'Preparar' funciona (logica pura); 'Rodar' vai falhar - por design."
        )

    return {
        "available": disponivel,
        "motor": motor,
        "path": local["path"],
        "local": local,
        "docker": docker,
        "via": via,
        "detail": detail,
        }


# ---------------------------------------------------------------------------
# manifesto dos pares no contrato do viewer 3D (window.__PAIRS__)
# ---------------------------------------------------------------------------
def split_pair_id(pair_id: str) -> "tuple[str, str]":
    """Deriva ``(isca, parceiro)`` do rotulo do par.

    Aceita ``__x__`` e ``_x_`` e descarta o sufixo de cofator (``_ATP``), do
    mesmo jeito que o gerador do viewer estatico -- os dois lados precisam
    rotular o par identicamente.
    """
    base = pair_id
    for sufixo in ("_ATP", "_atp"):
        if base.endswith(sufixo):
            base = base[: -len(sufixo)]
            break
    if "__x__" in base:
        isca, parceiro = base.split("__x__", 1)
    elif "_x_" in base:
        isca, parceiro = base.split("_x_", 1)
    else:
        isca, parceiro = base, ""
    return isca, parceiro


def pairs_manifest(
    sub: Subproject,
    *,
    cif_url: str = "/estrutura/{key}/{label}.cif",
    model: int = 0,
) -> "Dict[str, Any]":
    """Monta o manifesto que alimenta o viewer 3D, no MESMO contrato do pacote
    publicado (``window.__PAIRS__``).

    Junta o que ``observe_pairs`` ja calcula (indices de confianca + classe) com
    os residuos de interface de ``funnel.interface.iface_residues`` -- que sao o
    que o modo de cor "interface" do 3Dmol destaca. Assim o painel local e o
    um pacote estatico exportado mostram exatamente a mesma coisa, com um so JS
    (``web/static/viewer3d.js``).

    ``cif_url`` e o molde da URL por onde o navegador busca a estrutura (o
    painel serve o arquivo; o pacote estatico usa caminho relativo).

    Par que falhou na pontuacao entra com ``erro`` preenchido e sem interface:
    aparece na lista marcado, nunca sumido.
    """
    try:
        from funnel import interface as _iface
    except Exception:  # noqa: BLE001 - sem funnel, segue sem destaque de interface
        _iface = None  # type: ignore[assignment]

    pares: "List[Dict[str, Any]]" = []
    for linha in observe_pairs(sub, model=model):
        pair_id = str(linha.get("pair_id") or "")
        isca, parceiro = split_pair_id(pair_id)
        cif = linha.get("cif")
        info: "Dict[str, Any]" = {
            "iface_by_chain": {},
            "iface_chain_sizes": {},
            "iface_hetero_chains": [],
        }
        if cif and _iface is not None and not linha.get("erro"):
            try:
                ir = _iface.iface_residues(str(cif), strict=False)
                por_cadeia: "Dict[str, List[int]]" = {}
                if ir.get("partner_chain"):
                    por_cadeia[str(ir["partner_chain"])] = [
                        int(r) for r in ir.get("partner_residues", [])
                    ]
                for bc in ir.get("bait_chains", []):
                    atual = set(por_cadeia.get(str(bc), []))
                    por_cadeia[str(bc)] = sorted(
                        atual | {int(r) for r in ir.get("bait_residues", [])}
                    )
                info = {
                    "iface_by_chain": por_cadeia,
                    "iface_chain_sizes": {
                        str(k): int(v) for k, v in (ir.get("chain_sizes") or {}).items()
                    },
                    "iface_hetero_chains": [
                        str(c) for c in ir.get("hetero_chains", [])
                    ],
                }
            except Exception as exc:  # noqa: BLE001 - interface e opcional
                info["erro_interface"] = f"{type(exc).__name__}: {exc}"

        pares.append(
            {
                "label": pair_id,
                "isca": isca,
                "parceiro": parceiro,
                "classe": linha.get("classe") or "weak",
                "iptm": linha.get("iptm"),
                "iface_iptm": linha.get("iface_iptm"),
                "ipsae": linha.get("ipsae"),
                "ipsae_src": linha.get("ipsae_src"),
                "ptm": linha.get("ptm"),
                "confidence_score": linha.get("confidence_score"),
                "complex_plddt": linha.get("complex_plddt"),
                "iface_plddt": linha.get("iface_plddt"),
                "pae_iface_mean": linha.get("pae_iface_mean"),
                "bsa": linha.get("bsa"),
                "n_contacts": linha.get("n_contacts"),
                "n_iface_res": linha.get("n_iface_res"),
                "erro": linha.get("erro"),
                "cif": cif_url.format(key=sub.key, label=pair_id),
                "pdb": cif_url.format(key=sub.key, label=pair_id),
                **info,
            }
        )
    return {"key": sub.key, "titulo": sub.name, "pairs": pares}
