# -*- coding: utf-8 -*-
"""Orquestracao do funil inteiro de uma campanha (o "guarda-chuva" fechado).

Encadeia, para TODOS os pares de um subprojeto::

    scoring.build_all -> gate.gate_boltz -> [aprovados] ensure_split
                      -> haddock_adapter.prepare -> manifest.json por par
                      -> (opcional) haddock_adapter.run

Nada de GPU e nada de rede: o Boltz-2 ja rodou, e o que este modulo faz e ler o
disco, aplicar o portao e escrever `.cfg`/`ambig.tbl`. O unico passo que exige
binario externo e ``ate="run"``, e o erro do motor sobe **como veio** (sem
HADDOCK3 instalado ou sem a imagem do container, o erro e explicito - ver as
notas do YAML da campanha).

Onde escreve
------------
Tudo em ``paths.haddock_out`` do YAML da campanha (sob a raiz de dados do
usuario, FORA do repositorio), **um subdiretorio por par**::

    <haddock_out>/
        manifests/<pair_id>.json        <- envelope do par (o que a GUI le)
        <pair_id>/pdbs/receptor.pdb     <- BARNASE  (cadeia/segid A)
        <pair_id>/pdbs/ligand.pdb       <- BARSTAR (cadeia/segid C)
        <pair_id>/run-<bait>-<partner>.cfg
        <pair_id>/ambig.tbl

Nunca dentro do repositorio: codigo de um lado, DADO do outro. Nenhum
resultado de execucao entra no git (ver ``.gitignore``).

Idempotencia
------------
Rodar duas vezes reescreve os mesmos arquivos com o mesmo conteudo: os
manifestos sao remontados do disco a cada chamada (nao ha estado acumulado), o
split confere cadeia/segid do PDB que grava e o ``prepare`` do motor sobrescreve
`.cfg`/`.tbl`. Nao ha append em lugar nenhum, entao nada duplica.

Papeis de cadeia
----------------
Vem do bloco ``chain_roles`` do YAML da campanha e sao repassados a
``ensure_split``/``prepare``. Sem eles, o DEMO01 poria a BARSTAR no
``receptor.pdb`` de um par cuja isca e a barnase - ver `funnel.interface`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping

from core.data import Subproject

from .contract import HaddockArtifacts, PairManifest, save_manifest
from .gate import gate_boltz
from .scoring import build_all_report, resolve_subproject

__all__ = ["ETAPAS", "PipelineError", "run_funnel"]

#: Ate onde o funil vai. Ordem de progressao (cada um inclui o anterior).
ETAPAS: "tuple[str, ...]" = ("gate", "prepare", "run")


class PipelineError(RuntimeError):
    """Falha de orquestracao (config incompleta, etapa invalida, destino ausente)."""


def _raiz_de_saida(
    sub: Subproject, out_root: "str | Path | None", workdir: "str | Path | None"
) -> Path:
    """Resolve a raiz de saida: argumento explicito > ``paths.haddock_out`` do YAML.

    Sem nenhum dos dois e erro: o funil nao escolhe um destino plausivel no disco
    do usuario por conta propria (mesma regra do ``_workdir`` da ponte).
    """
    escolhido = out_root or workdir or sub.work_dir
    if not escolhido:
        raise PipelineError(
            f"{sub.key}: sem destino de saida - informe out_root/workdir ou declare "
            "'paths.haddock_out' no YAML da campanha."
        )
    return Path(str(escolhido))


def _pasta_manifestos(sub: Subproject, raiz: Path, *, raiz_explicita: bool) -> Path:
    """Onde gravar os ``<pair_id>.json`` (o que a GUI le como fonte do veredicto).

    Com raiz de saida EXPLICITA (out_root/workdir no pedido), os manifestos vao
    junto dela: escrever no ``paths.manifests`` da campanha enquanto os `.cfg`
    saem noutro lugar deixaria as duas metades do resultado em disco separado.
    Sem raiz explicita, vale o que a campanha declara.
    """
    if not raiz_explicita and sub.manifests_dir:
        return Path(sub.manifests_dir)
    return raiz / "manifests"


def run_funnel(
    subproject: "str | Subproject | Mapping[str, Any]",
    *,
    ate: str = "prepare",
    out_root: "str | Path | None" = None,
    workdir: "str | Path | None" = None,
    criteria: "Mapping[str, Any] | None" = None,
) -> "Dict[str, Any]":
    """Roda o funil de uma campanha inteira e devolve o resumo do que aconteceu.

    Parametros
    ----------
    subproject : nome da campanha (``DEMO01``/``DEMO01_barnase_barstar``), o ``Subproject``
        ou o dict de config ja lido.
    ate : ``"gate"`` para so pontuar+portar; ``"prepare"`` (padrao) para tambem
        gerar ``.cfg``/``ambig.tbl`` dos APROVADOS; ``"run"`` para tentar o
        HADDOCK3 de verdade (exige o binario; o erro do motor sobe sem mascara).
    out_root / workdir : raiz de saida. ``workdir`` existe porque e o nome que a
        ponte HTTP usa em ``POST /funnel/run``; os dois significam a mesma coisa.
    criteria : limiares para sobrescrever os de ``config/funnel.yaml``.

    Retorno (JSON puro, consumido pela ponte, pela GUI e pelo n8n)::

        {
          "subproject", "campaign", "ate", "out_root",
          "n_total", "n_aprovados", "n_reprovados", "n_erros",
          "aprovados":  [<PairManifest serializado>, ...],
          "reprovados": [<PairManifest serializado>, ...],
          "reprovados_resumo": [{"label", "reasons"}, ...],
          "artefatos":  [{"label", "cfg", "ambig_tbl", "receptor_pdb",
                          "ligand_pdb", "n_ativos_bait", "n_ativos_partner"}, ...],
          "erros": [{"pair_id", "error"}, ...],
          "manifests_dir": "..."
        }

    ``aprovados``/``reprovados`` sao manifestos SERIALIZADOS (e nao so o rotulo)
    porque e desse formato que ``core.data.manifests_from_response`` monta a
    tabela do funil; o resumo curto pedido pelo operador esta em
    ``reprovados_resumo``. Erro em UM par nao contamina os demais: ele entra em
    ``erros`` com a mensagem inteira e a contagem fica visivel no resumo.
    """
    etapa = str(ate).strip().lower()
    if etapa not in ETAPAS:
        raise PipelineError(f"ate={ate!r} invalido (esperado um de {list(ETAPAS)})")

    sub = resolve_subproject(subproject)
    raiz = _raiz_de_saida(sub, out_root, workdir)
    pasta_manifestos = _pasta_manifestos(
        sub, raiz, raiz_explicita=bool(out_root or workdir)
    )
    papeis = sub.chain_roles or None

    manifestos, erros = build_all_report(sub)
    # pares da planilha que nem chegaram a virar manifesto (run ausente do disco)
    n_total = len(manifestos) + len(erros)

    aprovados: "List[Dict[str, Any]]" = []
    reprovados: "List[Dict[str, Any]]" = []
    resumo_reprovados: "List[Dict[str, Any]]" = []
    artefatos: "List[Dict[str, Any]]" = []

    for m in manifestos:
        try:
            m.gate_boltz = gate_boltz(m.scores, criteria)
            if not m.gate_boltz.approved:
                m.stage = "rejected"
                reprovados.append(m.to_dict())
                resumo_reprovados.append(
                    {"label": m.pair_id, "reasons": list(m.gate_boltz.reasons)}
                )
                save_manifest(m, pasta_manifestos / f"{m.pair_id}.json")
                continue

            m.stage = "gated"
            if etapa != "gate":
                artefatos.append(_preparar(m, raiz, papeis, rodar=(etapa == "run")))
            aprovados.append(m.to_dict())
            save_manifest(m, pasta_manifestos / f"{m.pair_id}.json")
        except Exception as exc:  # noqa: BLE001 - um par ruim nao derruba o lote
            erros.append({"pair_id": m.pair_id, "error": f"{type(exc).__name__}: {exc}"})

    erros.sort(key=lambda e: str(e.get("pair_id")))
    return {
        "subproject": sub.key,
        "campaign": sub.campaign,
        "ate": etapa,
        "out_root": str(raiz),
        "manifests_dir": str(pasta_manifestos),
        "n_total": n_total,
        "n_manifestos": len(manifestos),
        "n_aprovados": len(aprovados),
        "n_reprovados": len(reprovados),
        "n_erros": len(erros),
        "aprovados": aprovados,
        "reprovados": reprovados,
        "reprovados_resumo": resumo_reprovados,
        "artefatos": artefatos,
        "erros": erros,
    }


def _preparar(
    m: PairManifest,
    raiz: Path,
    papeis: "Dict[str, Any] | None",
    *,
    rodar: bool,
) -> "Dict[str, Any]":
    """Split + interface + ``.cfg``/``ambig.tbl`` de UM par aprovado.

    O manifesto e MUTADO no lugar (ganha ``receptor_pdb``/``ligand_pdb``/
    ``reference_pdb``,
    residuos ativos, ``haddock`` e ``stage``), para o chamador grava-lo ja
    completo. Com ``rodar=True`` tambem dispara o HADDOCK3 e devolve o envelope
    do motor em ``engine`` - inclusive quando ele falha (nada de mascarar: o
    binario ausente aparece como ``status="error"`` com a mensagem original).
    """
    from . import haddock_adapter

    destino = raiz / m.pair_id
    destino.mkdir(parents=True, exist_ok=True)

    relatorio = haddock_adapter.ensure_split(m, destino / "pdbs", chain_roles=papeis)

    if rodar:
        resultado = haddock_adapter.run(m, destino, chain_roles=papeis)
        artefatos = HaddockArtifacts(
            cfg=resultado.outputs.get("cfg"),
            ambig_tbl=resultado.outputs.get("ambig_tbl"),
            run_dir=str(haddock_adapter.expected_run_dir(m, destino)),
        )
        engine = {
            "status": getattr(resultado, "status", None),
            "error": getattr(resultado, "error", None),
        }
    else:
        artefatos = haddock_adapter.prepare(m, destino, chain_roles=papeis)
        engine = None

    m.haddock = artefatos
    m.stage = "docked" if (rodar and engine and engine.get("status") == "ok") else "prepared"
    return {
        "label": m.pair_id,
        "cfg": artefatos.cfg,
        "ambig_tbl": artefatos.ambig_tbl,
        "receptor_pdb": m.receptor_pdb,
        "ligand_pdb": m.ligand_pdb,
        # complexo A+C em PDB: e ele o reference_fname do caprieval (nunca o .cif)
        "reference_pdb": m.reference_pdb,
        "n_ativos_bait": len(m.active_residues),
        "n_ativos_partner": len(m.partner_active_residues),
        "papeis_fonte": relatorio.get("papeis_fonte"),
        "engine": engine,
    }
