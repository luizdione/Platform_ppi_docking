# -*- coding: utf-8 -*-
"""Funil de confianca: os dois portoes que decidem o que gasta GPU.

Posicao no guarda-chuva::

    Boltz-2 (ja rodado) -> score -> [GATE 1] -> HADDOCK3 -> [GATE 2] -> [MD futuro]

* **GATE 1** (``gate_boltz``) - roda sobre as metricas do Boltz-2 e decide quem
  merece uma rodada de HADDOCK3. Codifica o criterio que, antes do funil, era
  aplicado A MAO par por par: **ipSAE >= 0.15 E cobertura da face nuclear
  >= 50% E face canonica**. Os tres limiares sao ajustaveis em
  ``config/funnel.yaml`` -- recalibre-os para o seu sistema.
* **GATE 2** (``gate_haddock``) - roda sobre o ``capri_clt.tsv`` do HADDOCK3 ja
  parseado e decide quem merece dinamica molecular (fora do escopo por ora).

Regras de projeto obedecidas aqui:

* **Nunca degradar em silencio.** Limiar desconhecido no YAML, tipo invalido ou
  tabela CAPRI vazia levantam erro/reprovacao EXPLICITA - jamais um veredicto
  plausivel e errado.
* **Nunca aprovar por omissao.** Criterio decisivo ausente (``None``) reprova e
  aparece em ``reasons`` como dado FALTANTE.
* **Nunca reprovar em silencio.** Todo criterio avaliado - aprovado, reprovado
  ou faltante - gera uma linha em ``reasons`` com o valor real observado.
* Modulo **puro**: o unico I/O e a leitura do YAML de limiares.

A logica HIT/MAYBE/weak do motor de pontuacao NAO e reimplementada aqui: ver
``classify_with_ppi``, que delega a ``scoring_engine.rank.classify``.
"""

from __future__ import annotations

import warnings
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from .contract import GateResult, Scores

__all__ = [
    "DEFAULT_CRITERIA",
    "CRITERIA_PATH",
    "load_criteria",
    "gate_boltz",
    "read_capri_clt",
    "gate_haddock",
    "classify_with_ppi",
]


# --------------------------------------------------------------------------
# limiares padrao (usados quando config/funnel.yaml nao existe)
# --------------------------------------------------------------------------
#: Limiares embutidos. Fontes de cada numero:
#:
#: * ``ipsae_min`` / ``coverage_min`` / ``require_canonical_face`` - vem da
#:   curadoria manual que precedeu o funil. Sao limiares de TRIAGEM, nao
#:   constantes universais: ipSAE 0.15 serve para separar interface real de
#:   contato incidental, e deve ser recalibrado por sistema.
#: * ``iface_iptm_min`` (0.50) / ``pae_iface_max`` (10.0 A) / ``min_contacts``
#:   (10) - **lidos de ``scoring_engine.config.SCORE``** em tempo de import (ver
#:   ``_limiares_do_motor``), nao copiados a mao: o guarda-chuva acompanha o
#:   motor em vez de divergir em silencio quando o motor mudar um limiar.
#: * ``dockq_min`` (0.23) - limiar CAPRI de modelo "acceptable" (Basu & Wallner,
#:   PLoS ONE 2016): DockQ < 0.23 = incorrect; 0.23-0.49 acceptable; 0.49-0.80
#:   medium; >= 0.80 high. 0.23 e o piso para o modelo valer o custo de MD.
#: * ``irmsd_max`` (4.0 A) - mesma fronteira "acceptable" no criterio classico
#:   CAPRI de iRMSD (interface RMSD <= 4 A). E a expressao redundante do mesmo
#:   corte: por isso o padrao aprova por DockQ **ou** iRMSD.
#: * ``require_both_capri`` (False) - se True, exige DockQ **e** iRMSD.
#: * ``strict_missing`` (False) - se True, qualquer metrica secundaria ausente
#:   reprova; se False, ausencia de secundaria vira aviso em ``reasons`` (os
#:   criterios DECISIVOS do gate 1 reprovam quando ausentes de qualquer modo).
#: * ``faces_aceitas`` (None) - lista branca de rotulos de face aceitos (ex.:
#:   ``["nuclear (canonica)"]``, como no YAML da GUI). ``None`` = usar a regra
#:   generica "o rotulo contem 'canonica'".
#: Limiares ESPELHADOS do motor: chave interna -> chave em ``scoring_engine.config.SCORE``.
#: Os nomes divergem (``iface_iptm_min`` aqui e ``iptm_maybe`` la), por isso o
#: mapa e explicito - nunca um ``dict.update`` cego.
_ESPELHO_PPI: "Dict[str, str]" = {
    "iface_iptm_min": "iptm_maybe",
    "pae_iface_max": "pae_iface_max",
    "min_contacts": "min_contacts",
}

#: Valores de reserva dos limiares espelhados, usados SO quando o motor nao esta
#: importavel (ambiente leve, sem numpy/biopython instalados). Sao exatamente os
#: numeros que o motor traz hoje - o fallback nao muda veredicto nenhum.
_FALLBACK_ESPELHO: "Dict[str, Any]" = {
    "iface_iptm_min": 0.50,
    "pae_iface_max": 10.0,
    "min_contacts": 10,
}


def _limiares_do_motor() -> "Dict[str, Any]":
    """Le de ``scoring_engine.config.SCORE`` os limiares que o guarda-chuva compartilha.

    ``scoring_engine.config`` e stdlib-puro (o ``ppi`` so carrega numpy/biopython nos
    modulos de analise), entao o import e barato. Se ele falhar - o pacote nao
    esta instalado neste ambiente -, caimos em ``_FALLBACK_ESPELHO``, que traz
    os MESMOS numeros de hoje: o funil continua funcionando sem o motor.

    Chave presente no mapa mas ausente/invalida no motor emite
    ``RuntimeWarning`` e cai no fallback - divergencia real do contrato nao pode
    passar calada (o pacote esta la, so mudou de forma).
    """
    fora = dict(_FALLBACK_ESPELHO)
    try:
        # import barato: scoring_engine.config e stdlib-puro
        from scoring_engine.config import SCORE
    except Exception:  # noqa: BLE001 - ambiente sem o motor instalado
        return fora
    for interna, chave in _ESPELHO_PPI.items():
        if chave not in SCORE:
            warnings.warn(
                f"scoring_engine.config.SCORE nao traz '{chave}' (esperado por "
                f"'{interna}') - usando o fallback {fora[interna]!r}. "
                "O motor mudou de contrato: revise _ESPELHO_PPI.",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        try:
            fora[interna] = type(_FALLBACK_ESPELHO[interna])(SCORE[chave])
        except (TypeError, ValueError):
            warnings.warn(
                f"scoring_engine.config.SCORE['{chave}'] = {SCORE[chave]!r} nao e "
                f"conversivel para o tipo de '{interna}' - usando o fallback "
                f"{fora[interna]!r}.",
                RuntimeWarning,
                stacklevel=2,
            )
    return fora


DEFAULT_CRITERIA: "Dict[str, Any]" = {
    "ipsae_min": 0.15,
    "coverage_min": 0.5,
    "require_canonical_face": True,
    "faces_aceitas": None,
    # espelhados do motor (scoring_engine.config.SCORE) - ver _limiares_do_motor
    **_limiares_do_motor(),
    "dockq_min": 0.23,
    "irmsd_max": 4.0,
    "require_both_capri": False,
    "strict_missing": False,
}

#: Caminho padrao do YAML de limiares: ``<raiz do repo>/config/funnel.yaml``.
CRITERIA_PATH: Path = Path(__file__).resolve().parent.parent / "config" / "funnel.yaml"

# tipo esperado de cada chave (bool antes de float: bool e subclasse de int).
# ``list`` marca a lista-branca opcional de faces.
_SPEC: "Dict[str, type]" = {
    "ipsae_min": float,
    "coverage_min": float,
    "require_canonical_face": bool,
    "faces_aceitas": list,
    "iface_iptm_min": float,
    "pae_iface_max": float,
    "min_contacts": int,
    "dockq_min": float,
    "irmsd_max": float,
    "require_both_capri": bool,
    "strict_missing": bool,
}

#: Marcador de face canonica nos dados reais ("nuclear (canonica)" vs
#: "periferica (atipica)" vs celula vazia).
_CANONICA = "canonica"


# --------------------------------------------------------------------------
# leitura dos limiares
# --------------------------------------------------------------------------
def _coerce_criterio(nome: str, valor: "Any") -> "Any":
    """Valida/converte UM limiar segundo ``_SPEC`` (falha alto se nao der)."""
    esperado = _SPEC[nome]
    if esperado is list:
        if valor is None:
            return None
        if isinstance(valor, (str, bytes)) or not hasattr(valor, "__iter__"):
            raise TypeError(
                f"funnel.yaml: '{nome}' deve ser uma lista de rotulos, veio {valor!r}"
            )
        rotulos = [str(v).strip() for v in valor if str(v).strip()]
        return rotulos or None
    if esperado is bool:
        if not isinstance(valor, bool):
            raise TypeError(
                f"funnel.yaml: '{nome}' deve ser booleano (true/false), veio {valor!r}"
            )
        return valor
    if isinstance(valor, bool):
        raise TypeError(f"funnel.yaml: '{nome}' deve ser numerico, veio booleano {valor!r}")
    try:
        return esperado(valor)
    except (TypeError, ValueError) as exc:
        raise TypeError(
            f"funnel.yaml: '{nome}' deve ser {esperado.__name__}, veio {valor!r}"
        ) from exc


# Dialeto ANINHADO (o funnel.yaml versionado, que tambem serve de texto para a
# aba Ajuda da GUI): gate_boltz/gate_haddock -> criterios -> <nome> -> valor.
# Mapa "nome no YAML documentado" -> "chave interna".
_ALIAS_YAML: "Dict[str, str]" = {
    "ipsae_min": "ipsae_min",
    "coverage_core_face_min": "coverage_min",
    "coverage_min": "coverage_min",
    "faces_aceitas": "faces_aceitas",
    "require_canonical_face": "require_canonical_face",
    "iface_iptm_min": "iface_iptm_min",
    "pae_iface_max": "pae_iface_max",
    "min_contacts": "min_contacts",
    "dockq_min": "dockq_min",
    "irmsd_max": "irmsd_max",
    "require_both_capri": "require_both_capri",
    "strict_missing": "strict_missing",
}

# Blocos do dialeto aninhado que NAO sao criterios deste modulo (documentacao,
# parametros do engine). Ignorados de proposito - sem eles, um funnel.yaml
# legitimo derrubaria o carregamento.
_BLOCOS_NAO_CRITERIO = {"haddock3", "descricao", "observacao", "ausencia", "texto"}


def _achatar_yaml(dados: "Mapping[str, Any]", alvo: Path) -> "Dict[str, Any]":
    """Reduz o dialeto ANINHADO ao dicionario plano de limiares.

    Aceita as duas formas do arquivo:

    * **plana** - ``ipsae_min: 0.15`` direto na raiz;
    * **documentada** - ``gate_boltz: {criterios: {ipsae_min: {valor: 0.15,
      texto: "..."}}}``, que e a que a GUI exibe na aba Ajuda.

    ``valor: null`` no dialeto documentado significa "limiar ainda nao acordado":
    cai no default embutido e emite ``RuntimeWarning`` dizendo qual numero
    entrou no lugar - nunca um veredicto sem criterio conhecido, nunca silencio.
    """
    plano: "Dict[str, Any]" = {}
    nulos: "List[str]" = []
    desconhecidas: "List[str]" = []

    def _absorver(nome: "Any", valor: "Any") -> None:
        chave = str(nome).strip()
        if chave in _BLOCOS_NAO_CRITERIO:
            return
        interna = _ALIAS_YAML.get(chave)
        if interna is None:
            desconhecidas.append(chave)
            return
        # forma documentada: {valor: X, texto: "..."}
        if isinstance(valor, Mapping):
            if "valor" not in valor:
                desconhecidas.append(f"{chave} (sem chave 'valor')")
                return
            valor = valor["valor"]
        if valor is None:
            nulos.append(f"{chave}={DEFAULT_CRITERIA[interna]!r}")
            return
        plano[interna] = _coerce_criterio(interna, valor)

    for nome, valor in dados.items():
        chave = str(nome).strip()
        if chave in _BLOCOS_NAO_CRITERIO:
            continue
        if chave.startswith("gate_") and isinstance(valor, Mapping):
            criterios = valor.get("criterios", {})
            if not isinstance(criterios, Mapping):
                raise ValueError(
                    f"criterios invalidos em {alvo}: '{chave}.criterios' deveria ser "
                    f"um mapeamento, veio {type(criterios).__name__}"
                )
            for sub_nome, sub_valor in criterios.items():
                _absorver(sub_nome, sub_valor)
            continue
        _absorver(nome, valor)

    if desconhecidas:
        raise ValueError(
            f"criterios desconhecidos em {alvo}: {sorted(set(desconhecidas))} "
            f"(validos: {sorted(_ALIAS_YAML)})"
        )
    if nulos:
        warnings.warn(
            f"{alvo}: limiar(es) sem valor definido ({', '.join(sorted(nulos))}) - "
            "usando o default embutido indicado. Defina-os no YAML para o "
            "veredicto nao depender de um numero nao acordado.",
            RuntimeWarning,
            stacklevel=3,
        )
    return plano


def load_criteria(path: "str | Path | None" = None) -> "Dict[str, Any]":
    """Carrega os limiares do funil.

    Le ``config/funnel.yaml`` (ou o ``path`` informado) e sobrepoe os valores
    lidos aos de ``DEFAULT_CRITERIA``. Se o arquivo NAO existir, devolve uma
    copia dos defaults - esse e o unico caso de ausencia tolerado, e e
    intencional (o repo funciona sem YAML).

    Entende os dois dialetos do arquivo (ver ``_achatar_yaml``): o plano
    (``ipsae_min: 0.15``) e o documentado que a GUI exibe
    (``gate_boltz.criterios.ipsae_min.valor``).

    Falha alto - nunca degrada em silencio - quando:

    * o ``path`` foi informado EXPLICITAMENTE e nao existe (o usuario apontou
      um arquivo; sumir com o pedido dele seria degradar em silencio);
    * o YAML existe mas ``pyyaml`` nao esta instalado;
    * o YAML e malformado ou nao contem um mapeamento;
    * ha chave desconhecida (tipicamente um limiar com o nome errado, que
      passaria despercebido e mudaria a decisao de gasto de GPU);
    * algum valor tem tipo invalido.

    Retorna um ``dict`` novo (mutavel, seguro para o chamador ajustar).
    """
    explicito = path is not None
    alvo = Path(path) if explicito else CRITERIA_PATH

    criterios = dict(DEFAULT_CRITERIA)
    if not alvo.is_file():
        if explicito:
            raise FileNotFoundError(f"arquivo de criterios nao encontrado: {alvo}")
        return criterios

    try:
        import yaml  # import local: so e exigido quando existe YAML para ler
    except ImportError as exc:  # pragma: no cover - depende do ambiente
        raise ImportError(
            f"{alvo} existe mas 'pyyaml' nao esta instalado - instale pyyaml ou "
            "remova o arquivo para usar os limiares padrao (nao vou adivinhar limiar)."
        ) from exc

    try:
        dados = yaml.safe_load(alvo.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"criterios invalidos (YAML malformado): {alvo}") from exc

    if dados is None:  # arquivo vazio = "use os padroes", explicito o bastante
        return criterios
    if not isinstance(dados, Mapping):
        raise ValueError(
            f"criterios invalidos em {alvo}: esperava um mapeamento chave: valor, "
            f"veio {type(dados).__name__}"
        )

    criterios.update(_achatar_yaml(dados, alvo))
    return criterios


def _criterios(criteria: "Mapping[str, Any] | None") -> "Dict[str, Any]":
    """Normaliza o argumento ``criteria`` das funcoes de gate.

    ``None`` = carregar do YAML/defaults. Um mapeamento parcial e completado
    com os defaults (e validado do mesmo jeito que o YAML).
    """
    if criteria is None:
        return load_criteria()
    if not isinstance(criteria, Mapping):
        raise TypeError(
            f"criteria: esperava mapeamento ou None, veio {type(criteria).__name__}"
        )
    desconhecidas = sorted(set(map(str, criteria)) - set(_SPEC))
    if desconhecidas:
        raise ValueError(f"criterios desconhecidos: {desconhecidas} (validos: {sorted(_SPEC)})")
    fora = dict(DEFAULT_CRITERIA)
    for nome, valor in criteria.items():
        fora[str(nome)] = _coerce_criterio(str(nome), valor)
    return fora


# --------------------------------------------------------------------------
# GATE 1 - pos-Boltz
# --------------------------------------------------------------------------
def _as_scores(scores: "Scores | Mapping[str, Any]") -> Scores:
    """Aceita um ``Scores``, um dict ou qualquer dataclass com os mesmos campos."""
    if isinstance(scores, Scores):
        return scores
    if isinstance(scores, Mapping):
        return Scores.from_dict(scores)
    if is_dataclass(scores) and not isinstance(scores, type):
        conhecidos = {f.name for f in fields(Scores)}
        return Scores.from_dict(
            {f.name: getattr(scores, f.name) for f in fields(scores) if f.name in conhecidos}
        )
    raise TypeError(
        f"gate_boltz: esperava Scores/dict, veio {type(scores).__name__}"
    )


def _num(valor: "float | None") -> "float | None":
    """Descarta NaN (vem de planilha) tratando-o como ausencia explicita."""
    if valor is None:
        return None
    if valor != valor:  # NaN
        return None
    return float(valor)


def gate_boltz(
    scores: "Scores | Mapping[str, Any]",
    criteria: "Mapping[str, Any] | None" = None,
) -> GateResult:
    """GATE 1: decide se o par pos-Boltz merece uma rodada de HADDOCK3.

    Criterio DECISIVO (o do funil manual que o codigo substitui), os
    tres precisam passar:

    1. ``ipsae >= ipsae_min``;
    2. ``coverage_core_face >= coverage_min``;
    3. ``face`` aceita, se ``require_canonical_face``: quando o YAML declara
       ``faces_aceitas`` (ex.: ``["nuclear (canonica)"]``), o rotulo precisa
       casar com um item da lista; sem lista, vale a regra generica "o rotulo
       contem 'canonica'". Os rotulos sao os da SUA planilha: tipicamente um
       rotulo canonico, um ou mais atipicos, e a celula vazia.

    Um decisivo ausente (``None``) **reprova** e entra em ``reasons`` como dado
    FALTANTE - nunca ha aprovacao por omissao.

    Criterios CORROBORANTES (espelhando ``scoring_engine.config.SCORE``): ``iface_iptm``
    (ou ``iptm`` como reserva), ``pae_iface_mean`` e ``n_contacts``. Quando
    PRESENTES e fora do limiar, reprovam. Quando AUSENTES, o comportamento
    depende de ``strict_missing``: ``False`` (padrao) registra o aviso em
    ``reasons`` sem reprovar - alinhado ao motor, onde PAE ausente nao penaliza
    (``scoring_engine.rank.classify``) e onde as tabelas de ``avaliacoes/`` nem sempre
    trazem PAE/contatos; ``True`` reprova.

    Observacao honesta sobre contagens: ``n_contacts``/``n_iface_res`` sao
    ``int`` sem ``None`` no contrato, e um complexo com interface real nunca
    tem 0 contatos - portanto **0 e lido como "nao medido"**, nao como zero
    contatos, e cai na regra de dado faltante.

    Retorna um ``GateResult`` com ``reasons`` de TODOS os criterios avaliados
    e ``criteria`` com os limiares efetivamente usados (veredicto auditavel).
    """
    crit = _criterios(criteria)
    sc = _as_scores(scores)

    motivos: "List[str]" = []
    falhas: "List[str]" = []
    faltantes_secundarios: "List[str]" = []

    # --- 1. ipSAE (decisivo) ------------------------------------------------
    ipsae = _num(sc.ipsae)
    if ipsae is None:
        motivos.append("ipSAE FALTANTE (nao calculado) - criterio decisivo, reprova")
        falhas.append("ipsae")
    elif ipsae >= crit["ipsae_min"]:
        motivos.append(f"ipSAE {ipsae:.3f} >= {crit['ipsae_min']}")
    else:
        motivos.append(f"ipSAE {ipsae:.3f} < {crit['ipsae_min']}")
        falhas.append("ipsae")

    # --- 2. cobertura da face nuclear (decisivo) ------------------------------------
    cobertura = _num(sc.coverage_core_face)
    if cobertura is None:
        motivos.append(
            "cobertura da face nuclear FALTANTE (interface nao anotada) - criterio decisivo, reprova"
        )
        falhas.append("coverage_core_face")
    elif cobertura >= crit["coverage_min"]:
        motivos.append(f"cobertura da face nuclear {cobertura:.3f} >= {crit['coverage_min']}")
    else:
        motivos.append(f"cobertura da face nuclear {cobertura:.3f} < {crit['coverage_min']}")
        falhas.append("coverage_core_face")

    # --- 3. face canonica (decisivo, se exigido) ----------------------------
    face = sc.face.strip() if isinstance(sc.face, str) and sc.face.strip() else None
    if not crit["require_canonical_face"]:
        motivos.append(
            f"face '{face}' nao avaliada (require_canonical_face=false)"
            if face
            else "face nao avaliada (require_canonical_face=false)"
        )
    elif face is None:
        motivos.append("face FALTANTE (nao classificada) - criterio decisivo, reprova")
        falhas.append("face")
    else:
        aceitas = crit.get("faces_aceitas")
        if aceitas:
            alvo_face = face.lower()
            ok_face = any(str(a).strip().lower() in alvo_face for a in aceitas)
            motivos.append(
                f"face '{face}' {'esta' if ok_face else 'NAO esta'} na lista aceita "
                f"{list(aceitas)}"
            )
        else:
            ok_face = _CANONICA in face.lower()
            motivos.append(
                f"face '{face}' e canonica"
                if ok_face
                else f"face '{face}' nao e canonica (esperava conter '{_CANONICA}')"
            )
        if not ok_face:
            falhas.append("face")

    # --- 4. ipTM de interface (corroborante) --------------------------------
    ip = _num(sc.iface_iptm)
    rotulo_ip = "ipTM iface"
    if ip is None:
        ip = _num(sc.iptm)
        rotulo_ip = "ipTM global (iface ausente)"
    if ip is None:
        motivos.append("ipTM FALTANTE (nem iface_iptm nem iptm)")
        faltantes_secundarios.append("iface_iptm")
    elif ip >= crit["iface_iptm_min"]:
        motivos.append(f"{rotulo_ip} {ip:.3f} >= {crit['iface_iptm_min']}")
    else:
        motivos.append(f"{rotulo_ip} {ip:.3f} < {crit['iface_iptm_min']}")
        falhas.append("iface_iptm")

    # --- 5. PAE de interface (corroborante) ---------------------------------
    pae = _num(sc.pae_iface_mean)
    if pae is None:
        motivos.append("PAE de interface FALTANTE (nao calculado)")
        faltantes_secundarios.append("pae_iface_mean")
    elif pae <= crit["pae_iface_max"]:
        motivos.append(f"PAE iface {pae:.2f} A <= {crit['pae_iface_max']} A")
    else:
        motivos.append(f"PAE iface {pae:.2f} A > {crit['pae_iface_max']} A")
        falhas.append("pae_iface_mean")

    # --- 6. contatos (corroborante; 0 = nao medido) -------------------------
    n_contatos = int(sc.n_contacts or 0)
    if n_contatos <= 0:
        motivos.append("n_contacts FALTANTE (0 = interface nao medida, nao 'zero contatos')")
        faltantes_secundarios.append("n_contacts")
    elif n_contatos >= crit["min_contacts"]:
        motivos.append(f"contatos {n_contatos} >= {crit['min_contacts']}")
    else:
        motivos.append(f"contatos {n_contatos} < {crit['min_contacts']}")
        falhas.append("n_contacts")

    # --- consolidacao -------------------------------------------------------
    if faltantes_secundarios:
        if crit["strict_missing"]:
            motivos.append(
                "strict_missing=true: metricas secundarias ausentes "
                f"({', '.join(faltantes_secundarios)}) reprovam"
            )
            falhas.extend(faltantes_secundarios)
        else:
            motivos.append(
                "strict_missing=false: metricas secundarias ausentes "
                f"({', '.join(faltantes_secundarios)}) NAO bloqueiam, mas ficam registradas"
            )

    aprovado = not falhas
    motivos.append(
        "VEREDICTO: aprovado para HADDOCK3"
        if aprovado
        else f"VEREDICTO: reprovado ({', '.join(dict.fromkeys(falhas))})"
    )
    return GateResult(approved=aprovado, reasons=motivos, criteria=dict(crit))


# --------------------------------------------------------------------------
# GATE 2 - pos-HADDOCK3
# --------------------------------------------------------------------------
# Aliases de coluna do caprieval (o cabecalho do capri_clt.tsv varia entre
# versoes do HADDOCK3: "dockq"/"DockQ", "irmsd"/"i-RMSD"/"iRMSD"...).
_ALIAS_DOCKQ = ("dockq", "dock_q", "dockq_mean", "dockq_av")
_ALIAS_IRMSD = ("irmsd", "i-rmsd", "i_rmsd", "irmsd_mean", "irmsd_av")
_ALIAS_RANK = ("cluster_rank", "caprieval_rank", "rank", "cluster_id", "cluster-id", "model")


def _campo(linha: "Mapping[str, Any]", aliases: "Sequence[str]") -> "Any | None":
    """Busca a 1a coluna presente entre os aliases (case/espaco-insensivel)."""
    norm = {str(k).strip().lower().replace(" ", ""): v for k, v in linha.items()}
    for alias in aliases:
        if alias in norm:
            return norm[alias]
    return None


def _float_celula(valor: "Any") -> "float | None":
    """Celula do TSV -> float; vazio/'None'/'nan'/'-' viram None (nunca 0.0)."""
    if valor is None:
        return None
    if isinstance(valor, bool):
        raise TypeError(f"capri_clt: booleano nao e um valor numerico valido ({valor!r})")
    if isinstance(valor, str):
        texto = valor.strip()
        if texto == "" or texto.lower() in {"none", "nan", "na", "-", "--"}:
            return None
        valor = texto
    try:
        num = float(valor)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"capri_clt: valor nao conversivel para float ({valor!r})") from exc
    return None if num != num else num


def read_capri_clt(path: "str | Path") -> "List[Dict[str, Any]]":
    """Le o ``capri_clt.tsv`` do HADDOCK3 em linhas prontas para `gate_haddock`.

    POR QUE ESTA FUNCAO EXISTE (nao use `csv.DictReader` direto!)
    -------------------------------------------------------------
    O haddock3 escreve um BLOCO DE COMENTARIO antes do cabecalho -- 13 linhas
    iniciadas por ``#`` (titulo, `sortby_key`, `clt_threshold`, notas sobre
    `under_eval`). Um ``csv.DictReader`` ingenuo toma a PRIMEIRA linha
    (``########...``) como cabecalho: sai uma tabela de uma coluna so, sem
    `dockq` nem `irmsd`, e o `gate_haddock` -- corretamente, dada a entrada que
    recebeu -- REPROVA dizendo "nenhum cluster traz DockQ ou iRMSD". Ou seja: um
    veredicto plausivel e ERRADO sobre uma tabela perfeitamente valida,
    exatamente o modo de falha que o projeto proibe -- um par que deveria
    passar e reprovado por "dado faltante" que esta ali, legivel, duas linhas
    abaixo.

    Alem de pular os comentarios, esta funcao:

    * converte os campos numericos (`dockq`, `irmsd`, `lrmsd`, `fnat`, `score`,
      `bsa`, ...) para float;
    * traduz o placeholder ``"-"`` do haddock3 para ``None`` -- **nunca 0.0**
      (0.0 em `dockq` seria lido como "docking pessimo" e nao "nao medido");
    * preserva as colunas nao-numericas como texto.

    Levanta ``FileNotFoundError`` se o arquivo nao existe e ``ValueError`` se,
    descartados os comentarios, nao sobrar cabecalho -- nunca devolve lista
    vazia disfarcando arquivo corrompido/run interrompido.
    """
    import csv

    caminho = Path(path)
    if not caminho.is_file():
        raise FileNotFoundError(f"capri_clt.tsv nao encontrado: {caminho}")

    uteis = [
        ln for ln in caminho.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not uteis:
        raise ValueError(
            f"{caminho}: so ha linhas de comentario/vazias -- o caprieval nao "
            "chegou a escrever a tabela (run interrompido?)"
        )

    linhas: "List[Dict[str, Any]]" = []
    for bruta in csv.DictReader(uteis, delimiter="\t"):
        limpa: "Dict[str, Any]" = {}
        for chave, valor in bruta.items():
            if chave is None:
                continue
            texto = (valor or "").strip()
            if texto in {"", "-", "nan", "None"}:
                limpa[chave] = None
                continue
            try:
                limpa[chave] = float(texto)
            except ValueError:
                limpa[chave] = texto
        linhas.append(limpa)
    return linhas


def gate_haddock(
    capri_rows: "Iterable[Mapping[str, Any]]",
    criteria: "Mapping[str, Any] | None" = None,
) -> GateResult:
    """GATE 2: decide, pela saida do HADDOCK3, quem merece dinamica molecular.

    ``capri_rows`` = linhas do ``capri_clt.tsv`` JA parseadas. Nada de I/O aqui.
    Obtenha-as com `read_capri_clt(path)`: `csv.DictReader` direto no arquivo
    NAO funciona (o haddock3 escreve 13 linhas de comentario antes do cabecalho,
    e o gate acabaria reprovando por "dado faltante" -- ver `read_capri_clt`).

    **Melhor cluster** = o de maior DockQ (nao o de menor score HADDOCK: o
    veredicto do funil e sobre semelhanca com a referencia Boltz, nao sobre a
    energia empirica). Sem DockQ em nenhuma linha, cai para o de menor iRMSD.

    Limiares e sua justificativa:

    * ``dockq_min = 0.23`` - fronteira CAPRI entre "incorrect" e "acceptable"
      (Basu & Wallner, PLoS ONE 2016). Abaixo disso o modelo do HADDOCK nao
      reproduz a pose de referencia e nao vale o custo de MD.
    * ``irmsd_max = 4.0`` A - a MESMA fronteira "acceptable" no criterio
      classico CAPRI (interface RMSD). Por serem duas expressoes do mesmo
      corte, o padrao aprova quando **qualquer uma** passa; com
      ``require_both_capri: true`` exige as duas (mais conservador).

    Reprova - com motivo explicito - quando a tabela esta vazia ou quando nao
    ha DockQ nem iRMSD utilizavel (docking nao avaliado != docking ruim, mas
    tambem nao autoriza gastar GPU).
    """
    crit = _criterios(criteria)
    motivos: "List[str]" = []

    linhas = list(capri_rows or [])
    if not linhas:
        motivos.append("capri_clt VAZIO: nenhum cluster avaliado pelo caprieval - reprova")
        motivos.append("VEREDICTO: reprovado (sem clusters)")
        return GateResult(approved=False, reasons=motivos, criteria=dict(crit))

    for i, linha in enumerate(linhas):
        if not isinstance(linha, Mapping):
            raise TypeError(
                f"capri_rows[{i}]: esperava mapeamento (linha do TSV), "
                f"veio {type(linha).__name__}"
            )

    parsed = [
        {
            "rank": _campo(l, _ALIAS_RANK),
            "dockq": _float_celula(_campo(l, _ALIAS_DOCKQ)),
            "irmsd": _float_celula(_campo(l, _ALIAS_IRMSD)),
        }
        for l in linhas
    ]

    com_dockq = [p for p in parsed if p["dockq"] is not None]
    com_irmsd = [p for p in parsed if p["irmsd"] is not None]

    if com_dockq:
        melhor = max(com_dockq, key=lambda p: p["dockq"])
        motivos.append(
            f"{len(parsed)} cluster(s); melhor por DockQ = cluster {melhor['rank']!r}"
        )
    elif com_irmsd:
        melhor = min(com_irmsd, key=lambda p: p["irmsd"])
        motivos.append(
            f"{len(parsed)} cluster(s); DockQ ausente em todos - melhor por iRMSD = "
            f"cluster {melhor['rank']!r}"
        )
    else:
        motivos.append(
            f"{len(parsed)} cluster(s), mas NENHUM traz DockQ ou iRMSD "
            "(caprieval sem referencia?) - dado FALTANTE, reprova"
        )
        motivos.append("VEREDICTO: reprovado (sem metrica CAPRI)")
        return GateResult(approved=False, reasons=motivos, criteria=dict(crit))

    dockq, irmsd = melhor["dockq"], melhor["irmsd"]
    ok_dockq: "bool | None" = None
    ok_irmsd: "bool | None" = None

    if dockq is None:
        motivos.append("DockQ FALTANTE no melhor cluster")
    else:
        ok_dockq = dockq >= crit["dockq_min"]
        motivos.append(
            f"DockQ {dockq:.3f} {'>=' if ok_dockq else '<'} {crit['dockq_min']} "
            f"({'acceptable' if ok_dockq else 'incorrect'} pelo criterio CAPRI)"
        )

    if irmsd is None:
        motivos.append("iRMSD FALTANTE no melhor cluster")
    else:
        ok_irmsd = irmsd <= crit["irmsd_max"]
        motivos.append(
            f"iRMSD {irmsd:.2f} A {'<=' if ok_irmsd else '>'} {crit['irmsd_max']} A"
        )

    avaliados = [v for v in (ok_dockq, ok_irmsd) if v is not None]
    if crit["require_both_capri"]:
        if ok_dockq is None or ok_irmsd is None:
            motivos.append(
                "require_both_capri=true exige DockQ E iRMSD, e uma delas esta ausente - reprova"
            )
            aprovado = False
        else:
            aprovado = ok_dockq and ok_irmsd
            motivos.append("require_both_capri=true: exigidas DockQ E iRMSD")
    else:
        aprovado = any(avaliados)
        if len(avaliados) == 2 and ok_dockq != ok_irmsd:
            motivos.append(
                "ATENCAO: DockQ e iRMSD DIVERGEM sobre o mesmo cluster; "
                "require_both_capri=false aprova pela mais permissiva - conferir a referencia"
            )

    motivos.append(
        "VEREDICTO: aprovado (qualidade CAPRI suficiente para seguir)"
        if aprovado
        else "VEREDICTO: reprovado (qualidade CAPRI abaixo do aceitavel)"
    )
    return GateResult(approved=aprovado, reasons=motivos, criteria=dict(crit))


# --------------------------------------------------------------------------
# rotulo HIT/MAYBE/weak - delegado ao motor de pontuacao (nao reimplementar)
# --------------------------------------------------------------------------
def classify_with_ppi(scores: "Scores | Mapping[str, Any]") -> str:
    """Rotula o par como ``HIT``/``MAYBE``/``weak`` usando ``scoring_engine.rank.classify``.

    A logica NAO e duplicada aqui de proposito: se o motor mudar os limiares
    (``scoring_engine.config.SCORE``), o guarda-chuva acompanha automaticamente. Note que
    ``classify`` so olha ipTM/PAE/contatos - ele **nao conhece** ipSAE, face
    nem cobertura; por isso o rotulo e COMPLEMENTAR ao ``gate_boltz``, nunca
    substituto.

    O import e preguicoso (o motor puxa numpy/biopython). Se o import falhar
    - ambiente sem essas bibliotecas - devolve ``"desconhecido"`` e
    emite um ``RuntimeWarning`` explicito, em vez de explodir ou fingir um
    rotulo: o gate real continua sendo ``gate_boltz``.
    """
    sc = _as_scores(scores)
    try:
        # import local: numpy/biopython so quando preciso
        from scoring_engine import rank
    except Exception as exc:  # ImportError e afins do ambiente
        warnings.warn(
            "classify_with_ppi: nao foi possivel importar 'scoring_engine.rank' "
            f"({type(exc).__name__}: {exc}) - rotulo HIT/MAYBE/weak indisponivel "
            "(o motor de pontuacao exige numpy e biopython: pip install -e .). "
            "O veredicto do funil NAO depende deste rotulo.",
            RuntimeWarning,
            stacklevel=2,
        )
        return "desconhecido"
    return str(rank.classify(sc))
