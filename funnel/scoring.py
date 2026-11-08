# -*- coding: utf-8 -*-
"""Estagio "score": monta o `PairManifest` de um par a partir do que EXISTE em disco.

Posicao no funil::

    Boltz-2 (ja rodado) -> [SCORE] -> GATE 1 -> interface/split -> HADDOCK3 -> GATE 2

Este e o caminho de PRODUCAO que faltava: ele nao dispara predicao nenhuma e nao
recalcula metrica estrutural. Ele apenas COLETA o que ja foi produzido e embrulha
no envelope do contrato (`funnel.contract.PairManifest`), para o gate e o
HADDOCK3 receberem sempre a mesma forma.

De onde vem cada numero
-----------------------
* **Fonte primaria: a planilha de scores** declarada no YAML da campanha
  (``scores.tsv``, relativa a ``paths.avaliacoes``). A leitura e feita
  por ``core.data.load_scores_tsv`` -- a MESMA que o painel usa, para nao existir
  uma segunda verdade sobre coluna->campo nem sobre coercao de celula vazia.
* **Complemento: o ``confidence_<label>_model_0.json`` do run do Boltz**, lido
  como JSON puro (stdlib). Dele saem ``iptm`` e ``complex_plddt``.

  O campo de ipTM e o declarado em ``scores.iptm_field`` (tipicamente
  ``protein_iptm``), **nunca** o ``iptm`` global -- este ultimo inclui os
  ligantes/cofatores da predicao e infla o numero. A convencao de escala do
  pLDDT (0-1 -> 0-100) e a mesma de
  ``scoring_engine.score``, para os dois nao divergirem.

* **Nao calculado aqui:** ``pae_iface_mean``, ``bsa``, ``n_contacts``. Eles
  exigiriam reprocessar o ``.npz``/o modelo, o que ja e trabalho do motor de pontuacao;
  duplicar isso seria uma segunda implementacao de score. Ficam como ausencia
  explicita (``None``/0), e o ``gate_boltz`` os trata como metrica secundaria
  FALTANTE -- registrada nos motivos, nunca convertida em zero plausivel.

Regras obedecidas (as mesmas do resto do guarda-chuva)
-----------------------------------------------------
* Celula vazia / ``None`` / ``nan`` vira ``None``, **jamais 0.0**.
* Label fora da planilha, ``.cif`` inexistente ou campanha desconhecida levantam
  ``ScoringError`` com a lista do que existe -- nunca sai um manifesto
  meia-boca em silencio.
* Divergencia entre o ipTM da planilha e o do JSON do run vira ``RuntimeWarning``
  explicito (o JSON do run manda, por ser a fonte primaria do numero).

Papeis biologicos
-----------------
``bait``/``partner`` sao lidos das colunas de identidade declaradas em
``scores.bait_column``/``scores.partner_column`` da campanha. NAO se infere
papel do nome do label nem da ordem das cadeias do `.cif`: num complexo real
essa ordem pode estar invertida em relacao ao nome do par, e cadeias de
ligante costumam vir DEPOIS das proteinas.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from core.data import GuiError, Subproject, load_scores_tsv, load_subprojects

from .contract import PairManifest, Scores

__all__ = [
    "ScoringError",
    "resolve_subproject",
    "build_manifest",
    "build_all",
    "build_all_report",
    "score_pair",
    "score_subproject",
]


class ScoringError(RuntimeError):
    """Falha ao montar o manifesto de um par (mensagem ja legivel ao operador)."""


# --------------------------------------------------------------------------
# 1. resolucao da campanha
# --------------------------------------------------------------------------
def _apelidos(sub: Subproject) -> "set[str]":
    """Nomes pelos quais uma campanha pode ser chamada (tudo em minusculas).

    A mesma campanha e chamada de tres jeitos hoje: ``DEMO01`` (o ``id``, usado
    pelo workflow do n8n), ``DEMO01_barnase_barstar`` (o ``campaign``, usado pela ponte) e
    ``Barnase`` (o rotulo da GUI). Aceitar os tres aqui evita que o nome escolhido
    no pedido decida se o funil roda ou responde erro.
    """
    nomes = {sub.key, sub.campaign, sub.name, f"{sub.key}_{sub.name}"}
    return {str(n).strip().lower() for n in nomes if n}


def resolve_subproject(subproject: "str | Subproject | Mapping[str, Any]") -> Subproject:
    """Normaliza o argumento ``subproject`` para um ``Subproject``.

    Aceita o proprio objeto, o dict do YAML ja lido, ou o NOME da campanha
    (``id``/``campaign``/``label``). Nome desconhecido levanta ``ScoringError``
    listando as campanhas declaradas - nunca se escolhe uma campanha "parecida".
    """
    if isinstance(subproject, Subproject):
        return subproject
    if isinstance(subproject, Mapping):
        return Subproject.from_dict(subproject)
    if not isinstance(subproject, str) or not subproject.strip():
        raise ScoringError(
            "subproject: esperava o nome da campanha, um dict de config ou um "
            f"Subproject; veio {type(subproject).__name__}"
        )
    alvo = subproject.strip().lower()
    try:
        subs = load_subprojects()
    except GuiError as exc:
        raise ScoringError(str(exc)) from exc
    for s in subs:
        if alvo in _apelidos(s):
            return s
    conhecidas = ", ".join(sorted(f"{s.key}/{s.campaign}" for s in subs))
    raise ScoringError(
        f"campanha {subproject!r} nao declarada em config/subproject.*.yaml "
        f"(conhecidas: {conhecidas})"
    )


# --------------------------------------------------------------------------
# 2. leitura das colunas de identidade (bait/partner) da planilha
# --------------------------------------------------------------------------
def _bloco_scores(sub: Subproject) -> "Dict[str, Any]":
    """Bloco ``scores`` do YAML da campanha (vazio se nao declarado)."""
    bloco = sub.raw.get("scores")
    return dict(bloco) if isinstance(bloco, Mapping) else {}


def _identidades(sub: Subproject) -> "Dict[str, Tuple[str, str]]":
    """``{label: (bait, partner)}`` lido das colunas de identidade da planilha.

    As colunas vem de ``scores.bait_column``/``scores.partner_column`` no YAML.
    Se a campanha nao as declarar, devolve ``{}`` e o chamador cai nos nomes de
    molecula do bloco ``chain_roles`` (ver ``_papeis_biologicos``).

    Coluna declarada mas ausente do arquivo = ``ScoringError`` (declarar uma
    coluna que nao existe e erro de config, nao motivo para inventar um papel).
    """
    bloco = _bloco_scores(sub)
    col_bait = bloco.get("bait_column")
    col_partner = bloco.get("partner_column")
    if not (col_bait and col_partner):
        return {}
    if not sub.scores_tsv:
        raise ScoringError(
            f"{sub.key}: 'scores.bait_column/partner_column' declarados mas a "
            "campanha nao tem 'scores.tsv' de onde le-los"
        )
    caminho = Path(sub.scores_tsv)
    if not caminho.is_file():
        raise ScoringError(f"planilha de scores inexistente: {caminho}")
    col_label = dict(sub.scores_columns).get("label", "label")
    linhas = caminho.read_text(encoding="utf-8").splitlines()
    if not linhas:
        raise ScoringError(f"planilha de scores vazia: {caminho}")
    cabecalho = linhas[0].split("\t")
    faltando = [c for c in (col_label, col_bait, col_partner) if c not in cabecalho]
    if faltando:
        raise ScoringError(
            f"colunas de identidade ausentes em {caminho.name}: {faltando} "
            f"(cabecalho real: {cabecalho})"
        )
    i_label, i_bait, i_partner = (
        cabecalho.index(str(col_label)),
        cabecalho.index(str(col_bait)),
        cabecalho.index(str(col_partner)),
    )
    fora: "Dict[str, Tuple[str, str]]" = {}
    for linha in linhas[1:]:
        if not linha.strip():
            continue
        celulas = linha.split("\t")

        def _cel(i: int) -> str:
            return celulas[i].strip() if i < len(celulas) else ""

        rotulo = _cel(i_label)
        if rotulo:
            fora[rotulo] = (_cel(i_bait), _cel(i_partner))
    return fora


def _papeis_biologicos(
    sub: Subproject, label: str, identidades: "Mapping[str, Tuple[str, str]]"
) -> "Tuple[str, str]":
    """``(bait, partner)`` do par: planilha primeiro, ``chain_roles`` como reserva.

    Sem nenhuma das duas fontes e ``ScoringError``: preencher ``bait``/``partner``
    "no chute" (pela ordem das cadeias ou por um split do label) e exatamente o
    defeito dos papeis trocados que o DEMO01 documenta.
    """
    par = identidades.get(label)
    if par and par[0] and par[1]:
        return par
    papeis = sub.chain_roles
    receptor = (papeis.get("receptor") or {}) if isinstance(papeis, Mapping) else {}
    ligante = (papeis.get("ligand") or {}) if isinstance(papeis, Mapping) else {}
    bait = str(receptor.get("molecule") or "").strip()
    partner = str(ligante.get("molecule") or "").strip()
    if bait and partner:
        return bait, partner
    raise ScoringError(
        f"{label}: nao ha como saber quem e a isca e quem e o parceiro. Declare "
        f"'scores.bait_column'/'scores.partner_column' (colunas da planilha) ou "
        f"'chain_roles.receptor.molecule'/'chain_roles.ligand.molecule' no YAML "
        f"da campanha {sub.key}."
    )


# --------------------------------------------------------------------------
# 3. localizacao do run do Boltz em disco
# --------------------------------------------------------------------------
def _run_dir(sub: Subproject, label: str) -> "Path | None":
    """Diretorio ``<root>/<run_prefix><label>`` do par, se existir em disco."""
    for root_txt in sub.results_roots:
        candidato = Path(root_txt) / f"{sub.run_prefix}{label}"
        if candidato.is_dir():
            return candidato
    return None


def _predicao(run_dir: Path, label: str) -> Path:
    """Subdiretorio ``predictions/<label>`` (tolera um unico nome divergente)."""
    alvo = run_dir / "predictions" / label
    if alvo.is_dir():
        return alvo
    pasta = run_dir / "predictions"
    candidatos = sorted(p for p in pasta.glob("*") if p.is_dir()) if pasta.is_dir() else []
    if len(candidatos) == 1:
        return candidatos[0]
    raise ScoringError(
        f"{label}: 'predictions/<label>' ausente ou ambigua em {run_dir} "
        f"(subdiretorios encontrados: {[p.name for p in candidatos]})"
    )


def _localizar_modelo(sub: Subproject, label: str) -> "Tuple[Path, Path, Path | None]":
    """``(run_dir, cif, confidence_json|None)`` do par - falha alto se nao houver cif."""
    run_dir = _run_dir(sub, label)
    if run_dir is None:
        raise ScoringError(
            f"{label}: nenhum diretorio '{sub.run_prefix}{label}' em "
            f"{sub.results_roots} - o run do Boltz para este par nao esta neste disco."
        )
    pred = _predicao(run_dir, label)
    cif = pred / f"{pred.name}{sub.model_suffix}"
    if not cif.is_file():
        raise ScoringError(
            f"{label}: modelo ausente em disco: {cif} - sem o complexo do Boltz nao "
            "ha o que pontuar nem o que mandar para o HADDOCK3."
        )
    conf = pred / f"confidence_{pred.name}{sub.model_suffix.replace('.cif', '.json')}"
    return run_dir, cif, (conf if conf.is_file() else None)


def _ler_confianca(caminho: "Path | None", label: str) -> "Dict[str, Any]":
    """Le o ``confidence_*.json`` do Boltz (dict vazio se o arquivo nao existir).

    Ausencia vira ``RuntimeWarning`` explicito: os scores da planilha ainda
    servem, mas o operador precisa saber que o complemento do run nao entrou.
    """
    if caminho is None:
        warnings.warn(
            f"{label}: 'confidence_*.json' do run nao encontrado - ipTM e pLDDT "
            "virao apenas da planilha (ausencia continua sendo None, nunca 0.0).",
            RuntimeWarning,
            stacklevel=3,
        )
        return {}
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScoringError(f"{label}: confidence JSON malformado ({caminho}): {exc}") from exc
    if not isinstance(dados, dict):
        raise ScoringError(
            f"{label}: confidence JSON deveria ser um objeto, veio "
            f"{type(dados).__name__} ({caminho})"
        )
    return dados


# --------------------------------------------------------------------------
# 4. montagem dos Scores
# --------------------------------------------------------------------------
def _float_ou_none(valor: "Any") -> "float | None":
    """Numero utilizavel ou ``None`` (NaN e ausencia, nao zero)."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        num = float(valor)
    except (TypeError, ValueError):
        return None
    return None if num != num else num


def _montar_scores(
    label: str, planilha: "Mapping[str, Any]", confianca: "Mapping[str, Any]", campo_iptm: str
) -> Scores:
    """Junta planilha + JSON do run num ``Scores`` do contrato.

    O ipTM do JSON (``scores.iptm_field``, tipicamente ``protein_iptm``) e a
    fonte primaria; o da planilha entra como reserva. Divergencia entre os dois
    NAO e silenciada: vira ``RuntimeWarning`` com os dois numeros.
    """
    iptm_json = _float_ou_none(confianca.get(campo_iptm))
    iptm_tsv = _float_ou_none(planilha.get("iptm"))
    if iptm_json is not None and iptm_tsv is not None and abs(iptm_json - iptm_tsv) > 1e-3:
        warnings.warn(
            f"{label}: ipTM da planilha ({iptm_tsv}) diverge do "
            f"'{campo_iptm}' do run ({iptm_json}). Usando o do run (fonte primaria "
            "do numero); confira a planilha.",
            RuntimeWarning,
            stacklevel=3,
        )
    iptm = iptm_json if iptm_json is not None else iptm_tsv

    # mesma convencao de escala do motor (`scoring_engine.score`): o Boltz emite 0-1.
    plddt = _float_ou_none(confianca.get("complex_plddt"))
    if plddt is not None and plddt <= 1.0:
        plddt *= 100.0

    return Scores(
        iptm=iptm,
        # iface_iptm exigiria recortar o ipTM na interface (trabalho do motor):
        # ausencia explicita, nunca o ipTM global disfarcado de ipTM de interface.
        iface_iptm=None,
        ipsae=_float_ou_none(planilha.get("ipsae")),
        pae_iface_mean=None,   # nao recalculado aqui (ver docstring do modulo)
        complex_plddt=plddt,
        bsa=None,
        n_contacts=0,          # 0 = "nao medido" para o gate, nao "zero contatos"
        n_iface_res=int(planilha.get("n_iface_res") or 0),
        coverage_core_face=_float_ou_none(planilha.get("coverage_core_face")),
        face=(str(planilha["face"]).strip() or None) if planilha.get("face") else None,
    )


# --------------------------------------------------------------------------
# 5. API publica
# --------------------------------------------------------------------------
def build_manifest(
    label: str,
    subproject_cfg: "str | Subproject | Mapping[str, Any]",
    *,
    _planilha: "Mapping[str, Mapping[str, Any]] | None" = None,
    _ident: "Mapping[str, Tuple[str, str]] | None" = None,
) -> PairManifest:
    """Monta o ``PairManifest`` de UM par a partir do disco.

    ``label`` = o rotulo do run do Boltz (tambem a chave da planilha e o nome da
    pasta, sem o prefixo ``boltz_results_``).

    Preenche ``campaign``/``bait``/``partner`` com os papeis BIOLOGICOS corretos
    (no DEMO01, bait = barnase, partner = barstar), ``boltz_run_dir``/``cif_model``
    com os caminhos reais e ``scores`` com o que a planilha e o JSON do run
    trazem. ``stage`` fica ``"scored"``: o gate ainda nao rodou.

    Levanta ``ScoringError`` se o label nao estiver na planilha ou se o ``.cif``
    nao existir em disco. Os dois argumentos com ``_`` sao um cache interno de
    ``build_all`` (a planilha e lida UMA vez para o lote todo).

    Os PDBs do split e os residuos ativos NAO sao gerados aqui - eles so fazem
    sentido para quem passou no GATE 1, e sao trabalho de
    ``funnel.haddock_adapter.ensure_split`` (ver ``funnel.pipeline``).
    """
    sub = resolve_subproject(subproject_cfg)
    rotulo = str(label).strip()
    if not rotulo:
        raise ScoringError("label vazio: informe o rotulo do run do Boltz")

    planilha = _planilha if _planilha is not None else load_scores_tsv(sub)
    if rotulo not in planilha:
        raise ScoringError(
            f"{rotulo}: label ausente da planilha {sub.scores_tsv} "
            f"({len(planilha)} pares conhecidos). Sem linha na planilha nao ha "
            "ipSAE/cobertura/face - e o gate reprovaria por dado faltante."
        )
    identidades = _ident if _ident is not None else _identidades(sub)
    bait, partner = _papeis_biologicos(sub, rotulo, identidades)

    run_dir, cif, conf_path = _localizar_modelo(sub, rotulo)
    confianca = _ler_confianca(conf_path, rotulo)
    campo_iptm = str(_bloco_scores(sub).get("iptm_field") or "protein_iptm")

    return PairManifest(
        campaign=sub.campaign,
        pair_id=rotulo,
        bait=bait,
        partner=partner,
        boltz_run_dir=str(run_dir),
        cif_model=str(cif),
        scores=_montar_scores(rotulo, planilha[rotulo], confianca, campo_iptm),
        stage="scored",
    )


def build_all_report(
    subproject_cfg: "str | Subproject | Mapping[str, Any]",
) -> "Tuple[List[PairManifest], List[Dict[str, str]]]":
    """``(manifestos, falhas)`` de TODOS os pares da planilha da campanha.

    Percorre a planilha inteira (nao apenas os HITs ja aprovados a mao: quem
    decide e o GATE 1, sobre os numeros). Um par que nao puder ser montado - run
    ausente do disco, ``.cif`` faltando - NAO some: ele entra em ``falhas`` como
    ``{"pair_id": ..., "error": ...}`` e o chamador reporta a contagem real.
    """
    sub = resolve_subproject(subproject_cfg)
    planilha = load_scores_tsv(sub)
    if not planilha:
        raise ScoringError(
            f"{sub.key}: a campanha nao declara 'scores.tsv' (ou a planilha esta "
            "vazia) - sem ela nao ha ipSAE/cobertura/face para pontuar par nenhum."
        )
    identidades = _identidades(sub)
    manifestos: "List[PairManifest]" = []
    falhas: "List[Dict[str, str]]" = []
    for rotulo in planilha:
        try:
            manifestos.append(
                build_manifest(rotulo, sub, _planilha=planilha, _ident=identidades)
            )
        except (ScoringError, GuiError, ValueError, OSError) as exc:
            falhas.append({"pair_id": rotulo, "error": f"{type(exc).__name__}: {exc}"})
    manifestos.sort(key=lambda m: m.pair_id)
    falhas.sort(key=lambda f: f["pair_id"])
    return manifestos, falhas


def build_all(
    subproject_cfg: "str | Subproject | Mapping[str, Any]", *, strict: bool = True
) -> "List[PairManifest]":
    """Manifestos de todos os pares da campanha.

    Com ``strict=True`` (padrao), qualquer par que nao tenha sido montado faz a
    chamada inteira falhar, com a lista completa dos motivos - nada de devolver
    silenciosamente um lote menor do que a planilha. Use ``strict=False``
    (o que ``funnel.pipeline`` faz) quando as falhas devem virar linhas do
    relatorio em vez de interromper o lote.
    """
    manifestos, falhas = build_all_report(subproject_cfg)
    if falhas and strict:
        detalhe = "; ".join(f"{f['pair_id']}: {f['error']}" for f in falhas)
        raise ScoringError(
            f"{len(falhas)} de {len(falhas) + len(manifestos)} pares nao puderam "
            f"ser montados -> {detalhe}"
        )
    return manifestos


# --------------------------------------------------------------------------
# 6. nomes que a ponte HTTP resolve (bridge/app.py :: _OPS)
# --------------------------------------------------------------------------
def score_pair(subproject: "str | Subproject | Mapping[str, Any]", pair_id: str) -> Scores:
    """Metricas de UM par (contrato de ``POST /score`` com ``pair_id``).

    Devolve ``Scores`` porque e isso que a ponte publica sob a chave ``scores``.
    Para o envelope completo (caminhos do run, papeis, estagio) use
    ``build_manifest``.
    """
    return build_manifest(pair_id, subproject).scores


def score_subproject(
    subproject: "str | Subproject | Mapping[str, Any]",
) -> "List[PairManifest]":
    """Manifestos de todos os pares da campanha (contrato de ``POST /score``).

    Usa ``strict=False``: um par sem run no disco nao pode impedir a leitura dos
    outros 25. As falhas continuam visiveis - ``funnel.pipeline.run_funnel`` as
    reporta em ``erros`` e o ``POST /score`` simplesmente nao lista o par que nao
    existe no disco.
    """
    return build_all(subproject, strict=False)
