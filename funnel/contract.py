# -*- coding: utf-8 -*-
"""Contrato de dados do funil (envelope que atravessa todos os estagios).

Este modulo e a UNICA fonte da verdade sobre o formato que trafega entre os
estagios do guarda-chuva:

    Boltz-2 (ja rodado) -> score -> GATE 1 -> HADDOCK3 -> GATE 2 -> [MD futuro]

Regras de projeto:

* Tudo aqui e **JSON serializavel puro** (str/float/int/bool/list/dict/None),
  porque o manifesto e passado de no em no no n8n e gravado em disco entre
  estagios. Nada de objetos biopython, numpy ou Path dentro do envelope.
* Nenhum valor e inventado: campo ausente vira ``None`` (ou lista vazia), nunca
  um zero plausivel. Quem consome decide se ``None`` e motivo de reprovacao.
* ``from_dict`` e TOLERANTE a campos extras (ignora), mas ESTRITO quanto ao
  tipo do que reconhece: se vier lixo, levanta erro em vez de degradar em
  silencio.

Nao ha logica de gate/score/docking aqui - so o formato.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Mapping

__all__ = [
    "Scores",
    "GateResult",
    "HaddockArtifacts",
    "PairManifest",
    "STAGES",
    "save_manifest",
    "load_manifest",
]

# Estagios validos do campo PairManifest.stage, em ordem de progressao.
# "rejected" e terminal (o par saiu do funil em algum gate).
STAGES: tuple[str, ...] = ("scored", "gated", "prepared", "docked", "rejected")


# --------------------------------------------------------------------------
# helpers internos de coercao (nunca degradam em silencio)
# --------------------------------------------------------------------------
def _as_float(value: "Any | None", campo: str) -> "float | None":
    """Converte para float, aceitando None/'' como ausencia explicita."""
    if value is None:
        return None
    if isinstance(value, str):
        texto = value.strip()
        # celulas vazias das planilhas de avaliacao = ausencia, nao zero
        if texto == "" or texto.lower() in {"none", "nan", "na"}:
            return None
        value = texto
    if isinstance(value, bool):
        raise TypeError(f"{campo}: booleano nao e um valor numerico valido")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{campo}: nao e conversivel para float ({value!r})") from exc


def _as_int(value: "Any | None", campo: str, default: int = 0) -> int:
    """Converte para int; None vira o default declarado (contagens = 0)."""
    if value is None:
        return default
    if isinstance(value, bool):
        raise TypeError(f"{campo}: booleano nao e uma contagem valida")
    if isinstance(value, str):
        texto = value.strip()
        if texto == "":
            return default
        value = texto
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{campo}: nao e conversivel para int ({value!r})") from exc


def _as_str(value: "Any | None", campo: str) -> "str | None":
    """Normaliza para str ou None (string vazia vira None)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{campo}: esperava str, veio {type(value).__name__}")
    texto = value.strip()
    return texto or None


def _as_str_required(value: "Any | None", campo: str) -> str:
    """Campo textual obrigatorio: falha alto se vier vazio."""
    texto = _as_str(value, campo)
    if texto is None:
        raise ValueError(f"{campo}: campo obrigatorio ausente ou vazio")
    return texto


def _as_int_list(value: "Any | None", campo: str) -> "List[int]":
    """Lista de numeros de residuo (inteiros)."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise TypeError(f"{campo}: esperava lista de inteiros, veio {type(value).__name__}")
    return [_as_int(v, f"{campo}[{i}]") for i, v in enumerate(value)]


def _as_str_list(value: "Any | None", campo: str) -> "List[str]":
    """Lista de strings (ex.: motivos de um gate)."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
        raise TypeError(f"{campo}: esperava lista de strings, veio {type(value).__name__}")
    return [str(v) for v in value]


def _as_dict(value: "Any | None", campo: str) -> "Dict[str, Any]":
    """Dicionario livre (criterios do gate); precisa ser JSON serializavel."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{campo}: esperava objeto/dict, veio {type(value).__name__}")
    return {str(k): v for k, v in value.items()}


def _known(cls, data: "Mapping[str, Any]") -> "Dict[str, Any]":
    """Filtra o dict para os campos declarados no dataclass (ignora extras)."""
    nomes = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in nomes}


# --------------------------------------------------------------------------
# Scores - metricas estruturais de um par (Boltz-2 + funil manual do DEMO01)
# --------------------------------------------------------------------------
@dataclass
class Scores:
    """Metricas de confianca estrutural de UM par isca x parceiro.

    Os oito primeiros campos espelham `scoring_engine.score.Score` (mesma
    semantica, para poder ir e voltar sem traducao). Os dois ultimos vem da
    anotacao de interface da campanha (a planilha declarada em
    ``scores.tsv``):

    * ``coverage_core_face`` - fracao (0..1) dos residuos de interface que caem
      na sub-regiao NUCLEAR declarada da isca;
    * ``face`` - rotulo da face de interacao (ex.: "nuclear (canonica)").

    Ausencia e sempre ``None`` - nunca 0.0.
    """

    iptm: "float | None" = None
    iface_iptm: "float | None" = None
    ipsae: "float | None" = None
    pae_iface_mean: "float | None" = None
    complex_plddt: "float | None" = None
    bsa: "float | None" = None
    n_contacts: int = 0
    n_iface_res: int = 0
    coverage_core_face: "float | None" = None
    face: "str | None" = None

    def to_dict(self) -> "Dict[str, Any]":
        """Serializa para dict JSON-safe."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: "Mapping[str, Any]") -> "Scores":
        """Reconstroi a partir de dict (campos extras sao ignorados)."""
        d = _known(cls, data)
        return cls(
            iptm=_as_float(d.get("iptm"), "iptm"),
            iface_iptm=_as_float(d.get("iface_iptm"), "iface_iptm"),
            ipsae=_as_float(d.get("ipsae"), "ipsae"),
            pae_iface_mean=_as_float(d.get("pae_iface_mean"), "pae_iface_mean"),
            complex_plddt=_as_float(d.get("complex_plddt"), "complex_plddt"),
            bsa=_as_float(d.get("bsa"), "bsa"),
            n_contacts=_as_int(d.get("n_contacts"), "n_contacts"),
            n_iface_res=_as_int(d.get("n_iface_res"), "n_iface_res"),
            coverage_core_face=_as_float(d.get("coverage_core_face"), "coverage_core_face"),
            face=_as_str(d.get("face"), "face"),
        )


# --------------------------------------------------------------------------
# GateResult - veredicto de um portao do funil
# --------------------------------------------------------------------------
@dataclass
class GateResult:
    """Veredicto de um gate (GATE 1 pos-Boltz, GATE 2 pos-HADDOCK3).

    * ``approved`` - passou ou nao;
    * ``reasons`` - motivos LEGIVEIS, tanto de aprovacao quanto de reprovacao
      (ex.: "ipSAE 0.616 >= 0.15", "cobertura da face nuclear ausente");
    * ``criteria`` - os limiares e valores efetivamente usados, para o
      veredicto ser auditavel meses depois (ex.: {"ipsae_min": 0.15}).
    """

    approved: bool = False
    reasons: "List[str]" = field(default_factory=list)
    criteria: "Dict[str, Any]" = field(default_factory=dict)

    def to_dict(self) -> "Dict[str, Any]":
        """Serializa para dict JSON-safe."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: "Mapping[str, Any]") -> "GateResult":
        """Reconstroi a partir de dict (campos extras sao ignorados)."""
        d = _known(cls, data)
        aprovado = d.get("approved", False)
        if not isinstance(aprovado, bool):
            raise TypeError(f"approved: esperava bool, veio {type(aprovado).__name__}")
        return cls(
            approved=aprovado,
            reasons=_as_str_list(d.get("reasons"), "reasons"),
            criteria=_as_dict(d.get("criteria"), "criteria"),
        )


# --------------------------------------------------------------------------
# HaddockArtifacts - o que o estagio de docking produz
# --------------------------------------------------------------------------
@dataclass
class HaddockArtifacts:
    """Caminhos dos artefatos do HADDOCK3 para um par.

    Todos sao ``str | None`` (caminho absoluto como texto - Path nao e JSON):

    * ``cfg`` - arquivo .cfg gerado por ``Haddock3Engine.prepare`` (logica pura,
      nao exige o binario haddock3 instalado);
    * ``ambig_tbl`` - restricoes ambiguas (AIR) derivadas da interface do Boltz;
    * ``run_dir`` - diretorio de execucao do haddock3 (so existe apos ``run``);
    * ``capri_clt`` - tabela caprieval por cluster (DockQ/iRMSD contra o .cif do
      Boltz usado como referencia);
    * ``decision`` - veredicto textual curto do GATE 2, se ja calculado.
    """

    cfg: "str | None" = None
    ambig_tbl: "str | None" = None
    run_dir: "str | None" = None
    capri_clt: "str | None" = None
    decision: "str | None" = None

    def to_dict(self) -> "Dict[str, Any]":
        """Serializa para dict JSON-safe."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: "Mapping[str, Any]") -> "HaddockArtifacts":
        """Reconstroi a partir de dict (campos extras sao ignorados)."""
        d = _known(cls, data)
        return cls(
            cfg=_as_str(d.get("cfg"), "cfg"),
            ambig_tbl=_as_str(d.get("ambig_tbl"), "ambig_tbl"),
            run_dir=_as_str(d.get("run_dir"), "run_dir"),
            capri_clt=_as_str(d.get("capri_clt"), "capri_clt"),
            decision=_as_str(d.get("decision"), "decision"),
        )


# --------------------------------------------------------------------------
# PairManifest - o envelope completo de UM par
# --------------------------------------------------------------------------
@dataclass
class PairManifest:
    """Envelope de UM par isca x parceiro atravessando o funil inteiro.

    Campos de identidade (obrigatorios): ``campaign`` (a campanha/subprojeto,
    ex.: "DEMO01_barnase_barstar"), ``pair_id`` (identificador estavel do par, tipicamente
    o label do run Boltz), ``bait`` e ``partner``.

    ATENCAO (achado do recon do DEMO01): ``bait``/``partner`` sao rotulos
    BIOLOGICOS, NAO a ordem das cadeias no .cif - nesses runs a barnase (isca)
    esta na cadeia B e a barstar (parceiro) na cadeia A, com ATP/MG em C/D.
    Quem preencher ``active_residues``/``partner_active_residues`` deve resolver
    a identidade pela cadeia real, nunca pela posicao nem pelo nome do arquivo.

    ``active_residues`` = residuos de interface da ISCA (``bait``);
    ``partner_active_residues`` = residuos de interface do PARCEIRO.
    Ambos alimentam o ambig.tbl do HADDOCK3.

    ``reference_pdb`` = o COMPLEXO inteiro (receptor ``A`` + ligante ``C``) num
    unico PDB, gerado do mesmo ``cif_model`` por `funnel.split.reference_pdb`.
    E ele - nunca o ``.cif`` - que vira o ``reference_fname`` do bloco
    ``[caprieval]``: o haddock3 le a referencia como PDB de colunas fixas e um
    mmCIF quebra o modulo inteiro (ver a docstring de `funnel.split`).
    """

    campaign: str
    pair_id: str
    bait: str
    partner: str
    boltz_run_dir: "str | None" = None
    cif_model: "str | None" = None
    scores: Scores = field(default_factory=Scores)
    gate_boltz: GateResult = field(default_factory=GateResult)
    receptor_pdb: "str | None" = None
    ligand_pdb: "str | None" = None
    reference_pdb: "str | None" = None
    active_residues: "List[int]" = field(default_factory=list)
    partner_active_residues: "List[int]" = field(default_factory=list)
    haddock: HaddockArtifacts = field(default_factory=HaddockArtifacts)
    stage: str = "scored"

    def __post_init__(self) -> None:
        """Valida identidade e estagio na construcao (falha alto, nunca em silencio)."""
        self.campaign = _as_str_required(self.campaign, "campaign")
        self.pair_id = _as_str_required(self.pair_id, "pair_id")
        self.bait = _as_str_required(self.bait, "bait")
        self.partner = _as_str_required(self.partner, "partner")
        if self.stage not in STAGES:
            raise ValueError(
                f"stage invalido: {self.stage!r} (esperado um de {list(STAGES)})"
            )

    def to_dict(self) -> "Dict[str, Any]":
        """Serializa o envelope inteiro para dict JSON-safe (aninhado)."""
        return {
            "campaign": self.campaign,
            "pair_id": self.pair_id,
            "bait": self.bait,
            "partner": self.partner,
            "boltz_run_dir": self.boltz_run_dir,
            "cif_model": self.cif_model,
            "scores": self.scores.to_dict(),
            "gate_boltz": self.gate_boltz.to_dict(),
            "receptor_pdb": self.receptor_pdb,
            "ligand_pdb": self.ligand_pdb,
            "reference_pdb": self.reference_pdb,
            "active_residues": list(self.active_residues),
            "partner_active_residues": list(self.partner_active_residues),
            "haddock": self.haddock.to_dict(),
            "stage": self.stage,
        }

    @classmethod
    def from_dict(cls, data: "Mapping[str, Any]") -> "PairManifest":
        """Reconstroi o envelope a partir de dict (campos extras sao ignorados)."""
        if not isinstance(data, Mapping):
            raise TypeError(
                f"PairManifest.from_dict: esperava dict, veio {type(data).__name__}"
            )
        d = _known(cls, data)
        return cls(
            campaign=_as_str_required(d.get("campaign"), "campaign"),
            pair_id=_as_str_required(d.get("pair_id"), "pair_id"),
            bait=_as_str_required(d.get("bait"), "bait"),
            partner=_as_str_required(d.get("partner"), "partner"),
            boltz_run_dir=_as_str(d.get("boltz_run_dir"), "boltz_run_dir"),
            cif_model=_as_str(d.get("cif_model"), "cif_model"),
            scores=Scores.from_dict(_as_dict(d.get("scores"), "scores")),
            gate_boltz=GateResult.from_dict(_as_dict(d.get("gate_boltz"), "gate_boltz")),
            receptor_pdb=_as_str(d.get("receptor_pdb"), "receptor_pdb"),
            ligand_pdb=_as_str(d.get("ligand_pdb"), "ligand_pdb"),
            reference_pdb=_as_str(d.get("reference_pdb"), "reference_pdb"),
            active_residues=_as_int_list(d.get("active_residues"), "active_residues"),
            partner_active_residues=_as_int_list(
                d.get("partner_active_residues"), "partner_active_residues"
            ),
            haddock=HaddockArtifacts.from_dict(_as_dict(d.get("haddock"), "haddock")),
            stage=_as_str_required(d.get("stage", "scored"), "stage"),
        )


# --------------------------------------------------------------------------
# persistencia (JSON em disco - o que o n8n le/escreve entre nos)
# --------------------------------------------------------------------------
def save_manifest(m: PairManifest, path: "str | Path") -> Path:
    """Grava o manifesto como JSON (utf-8, indent=2). Cria o diretorio pai."""
    if not isinstance(m, PairManifest):
        raise TypeError(
            f"save_manifest: esperava PairManifest, veio {type(m).__name__}"
        )
    destino = Path(path)
    destino.parent.mkdir(parents=True, exist_ok=True)
    texto = json.dumps(m.to_dict(), ensure_ascii=False, indent=2)
    destino.write_text(texto + "\n", encoding="utf-8")
    return destino


def load_manifest(path: "str | Path") -> PairManifest:
    """Le um manifesto JSON do disco. Arquivo ausente/invalido = erro claro."""
    origem = Path(path)
    if not origem.is_file():
        raise FileNotFoundError(f"manifesto nao encontrado: {origem}")
    try:
        data = json.loads(origem.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifesto invalido (JSON malformado): {origem}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"manifesto invalido (esperava objeto JSON): {origem}")
    return PairManifest.from_dict(data)
