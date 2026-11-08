# -*- coding: utf-8 -*-
"""
funnel.air_homolog -- restricoes de interacao (AIR) derivadas de COMPLEXO
CRISTALOGRAFICO HOMOLOGO, para docking HADDOCK3 independente da predicao.

POR QUE ESTE MODULO EXISTE
--------------------------
O caminho padrao do funil (`funnel.interface.iface_residues`) extrai os residuos
de interface do PROPRIO modelo Boltz e os grava como AIR. Em seguida o
`caprieval` mede DockQ contra esse MESMO modelo. Isso e circular: informa-se ao
HADDOCK onde e a interface e pergunta-se se ele a encontrou. Na pratica o
resultado fica proximo do perfeito (fnat ~ 1,0 / i-RMSD < 0,5 A / DockQ > 0,95)
-- numero que so mostra que o HADDOCK funciona, nao que a interface e real.

Aqui as restricoes vem de OUTRA fonte: um complexo experimental HOMOLOGO,
resolvido por cristalografia. O DockQ resultante responde a pergunta certa --
"o docking guiado por informacao independente reconverge para a pose predita?"
-- e pode REFUTAR a predicao, o que o caminho circular nunca poderia.

O catalogo `TEMPLATES` abaixo e um PONTO DE PARTIDA de complexos publicos, nao
uma lista fechada: para o seu sistema, acrescente o(s) homologo(s) do seu par
(PDB ID + os dois chain_id) e use-o pelo `template_id`.

ARMADILHA TRATADA AQUI (nao ignorar)
------------------------------------
Quando a isca e o template divergem muito em tamanho -- insercoes, dominios
extra, caudas desordenadas -- alinhar as cadeias INTEIRAS produz um alinhamento
quase sequencial que atropela as insercoes, e a superposicao sai inutilizavel
(TM-score baixo com RMSD alto). Restringir ao CORE alinhavel por sequencia antes
da superposicao estrutural resolve. Por isso o fluxo e sempre
    alinhamento de sequencia -> recorte do core -> superposicao estrutural.

As insercoes do alvo que nao existem no template costumam cair DENTRO da regiao
de interface, e ai o homologo nao tem como informar sobre elas. E por isso que
`build_air` devolve `cobertura` e `residuos_nao_mapeados`: a fracao da interface
do template que NAO existe no molde e um dado do resultado, que tem de ir para
os Metodos, nao ser silenciada.
"""
from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Cache dos templates baixados do RCSB (env ``PPI_DOCK_TEMPLATE_DIR``).
# Sem a env, usa um subdiretorio do cache do usuario - nunca um disco fixo.
TEMPLATE_DIR_ENV = "PPI_DOCK_TEMPLATE_DIR"
CACHE_DIR = Path(
    os.environ.get(TEMPLATE_DIR_ENV, "").strip()
    or (Path.home() / ".cache" / "ppi_docking" / "templates")
)

# Portoes de qualidade do mapeamento. Abaixo destes valores o AIR nao e
# "independente", e ruido -- e ruido produz o mesmo fnat=0 que uma interface
# errada, tornando o resultado nao interpretavel.
TM_MIN = 0.50            # limiar convencional de "mesma dobra"
PERDA_MAX = 0.30         # fracao maxima da interface do template sem correspondencia
CUTOFF_IFACE = 5.0       # A, mesmo cutoff de funnel.interface.iface_residues

# Catalogo de complexos homologos. `bait`/`partner` sao chain_id NO ARQUIVO do
# RCSB, e nao ha garantia de que a isca seja a cadeia A -- confira sempre a
# entrada antes de acrescentar uma linha aqui.
#
# Os tres abaixo sao complexos PUBLICOS, de referencia na literatura de
# interacao proteina-proteina e presentes no Docking Benchmark; servem de
# exemplo e para testar a instalacao. NENHUM deles e um sistema em estudo:
# acrescente aqui o homologo do SEU par.
TEMPLATES: Dict[str, Dict[str, Any]] = {
    "1BRS": {"bait": "A", "partner": "D", "nucleotideo": None,
             "descricao": "barnase . barstar (complexo de alta afinidade)"},
    "1JTG": {"bait": "B", "partner": "D", "nucleotideo": None,
             "descricao": "beta-lactamase TEM-1 . BLIP (inibidor proteico)"},
    "3HFM": {"bait": "Y", "partner": "H", "nucleotideo": None,
             "descricao": "lisozima . Fab HyHEL-10 (antigeno-anticorpo)"},
}


@dataclass
class AirHomologo:
    """AIR projetado + a proveniencia que torna o resultado auditavel."""
    template: str
    template_descricao: str
    bait_residues: List[int] = field(default_factory=list)
    partner_residues: List[int] = field(default_factory=list)
    # qualidade do mapeamento da ISCA (bait)
    tm_score: float = 0.0
    rmsd: float = 0.0
    n_alinhados: int = 0
    identidade_seq: float = 0.0
    # o que se perdeu no caminho
    cobertura: float = 0.0
    residuos_nao_mapeados: List[int] = field(default_factory=list)
    # idem para o lado do parceiro
    tm_score_parceiro: float = 0.0
    cobertura_parceiro: float = 0.0
    # PASSIVOS: entram so como alvo possivel no ambig.tbl, nunca como `assign`.
    partner_passive: List[int] = field(default_factory=list)
    seed_controle: Optional[int] = None      # reprodutibilidade do embaralhado
    insercoes_alvo: List[Tuple[int, int]] = field(default_factory=list)
    avisos: List[str] = field(default_factory=list)
    fonte: str = "homologo_cristalografico"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Carregamento
# --------------------------------------------------------------------------- #
def _struc():
    import biotite.structure as struc
    import biotite.structure.io.pdbx as pdbx
    return struc, pdbx


def buscar_template(pdb_id: str, cache_dir: Optional[Path] = None) -> Path:
    """Baixa (ou reaproveita do cache) o mmCIF do RCSB."""
    import biotite.database.rcsb as rcsb
    d = Path(cache_dir or CACHE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    alvo = d / f"{pdb_id.upper()}.cif"
    if alvo.is_file() and alvo.stat().st_size > 0:
        return alvo
    rcsb.fetch(pdb_id.upper(), "cif", target_path=str(d), overwrite=False)
    if not alvo.is_file():
        raise RuntimeError(f"[air_homolog] download de {pdb_id} nao produziu {alvo}")
    return alvo


def _cadeia_proteica(caminho: Path, chain_id: str):
    struc, pdbx = _struc()
    arr = pdbx.get_structure(pdbx.CIFFile.read(str(caminho)), model=1)
    sel = (arr.chain_id == chain_id) & struc.filter_amino_acids(arr)
    sub = arr[sel]
    if sub.array_length() == 0:
        presentes = sorted(set(arr.chain_id.tolist()))
        raise ValueError(
            f"[air_homolog] cadeia '{chain_id}' vazia em {caminho.name} "
            f"(presentes: {presentes})")
    return sub


# --------------------------------------------------------------------------- #
# Mapeamento template -> alvo
# --------------------------------------------------------------------------- #
def _mapear(tpl_chain, tgt_chain, rotulo: str
            ) -> Tuple[Dict[int, int], Dict[str, Any]]:
    """{res_id_template: res_id_alvo} + metricas.

    Fluxo obrigatorio: alinhamento de SEQUENCIA para achar o core comum ->
    recorte -> superposicao ESTRUTURAL so no core. Alinhar as cadeias inteiras
    de proteinas com insercoes grandes produz superposicao sem sentido (ver
    docstring do modulo).
    """
    import numpy as np
    import biotite.sequence.align as align
    struc, _ = _struc()

    s_tpl = struc.to_sequence(tpl_chain)[0][0]
    s_tgt = struc.to_sequence(tgt_chain)[0][0]
    mat = align.SubstitutionMatrix.std_protein_matrix()
    aln = align.align_optimal(s_tpl, s_tgt, mat, gap_penalty=(-10, -1),
                              terminal_penalty=False)[0]
    ident = float(align.get_sequence_identity(aln))

    off_tpl = int(tpl_chain.res_id.min())
    off_tgt = int(tgt_chain.res_id.min())
    trace = aln.trace

    # mapa por sequencia (base) e insercoes do alvo
    mapa = {off_tpl + int(a): off_tgt + int(b)
            for a, b in trace if a != -1 and b != -1}
    insercoes: List[Tuple[int, int]] = []
    corr: Optional[List[int]] = None
    for a, b in trace:
        if a == -1 and b != -1:
            if corr is None:
                corr = [int(b), int(b)]
            else:
                corr[1] = int(b)
        else:
            if corr is not None and corr[1] - corr[0] >= 3:
                insercoes.append((corr[0] + off_tgt, corr[1] + off_tgt))
            corr = None
    if corr is not None and corr[1] - corr[0] >= 3:
        insercoes.append((corr[0] + off_tgt, corr[1] + off_tgt))

    # superposicao estrutural restrita ao core alinhado
    core_tgt = set(mapa.values())
    tgt_core = tgt_chain[np.isin(tgt_chain.res_id, list(core_tgt))]
    ca_f = tgt_core[tgt_core.atom_name == "CA"]
    ca_m = tpl_chain[tpl_chain.atom_name == "CA"]
    tm = rms = 0.0
    n_aln = 0
    try:
        fit, _tr, idx_f, idx_m = struc.superimpose_structural_homologs(
            ca_f, ca_m, max_iterations=20)
        n_aln = int(len(idx_f))
        rms = float(struc.rmsd(ca_f.coord[idx_f], fit.coord[idx_m]))
        tm = float(struc.tm_score(ca_f, fit, idx_f, idx_m))
        # refina o mapa com a correspondencia estrutural, que respeita as
        # insercoes melhor que o alinhamento de sequencia puro
        mapa_est = {int(ca_m.res_id[m]): int(ca_f.res_id[f])
                    for f, m in zip(idx_f, idx_m)}
        if len(mapa_est) >= 0.8 * len(mapa):
            mapa = {**mapa, **mapa_est}
    except Exception as exc:                     # noqa: BLE001
        warnings.warn(f"[air_homolog] superposicao de {rotulo} falhou: {exc}",
                      RuntimeWarning)

    return mapa, {"tm_score": round(tm, 4), "rmsd": round(rms, 3),
                  "n_alinhados": n_aln, "identidade_seq": round(ident, 4),
                  "insercoes": insercoes}


def _interface_template(gt_chain, pt_chain, cutoff: float = CUTOFF_IFACE
                        ) -> Tuple[List[int], List[int]]:
    """Residuos em contato < cutoff nos DOIS lados do complexo do template."""
    import numpy as np
    struc, _ = _struc()
    cell = struc.CellList(pt_chain, cell_size=cutoff)
    lado_g, lado_p = set(), set()
    for i in range(gt_chain.array_length()):
        viz = [j for j in cell.get_atoms(gt_chain.coord[i], radius=cutoff) if j >= 0]
        if viz:
            lado_g.add(int(gt_chain.res_id[i]))
            for j in viz:
                lado_p.add(int(pt_chain.res_id[j]))
    return sorted(lado_g), sorted(lado_p)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def build_air(template_id: str,
              target_cif: "str|Path",
              bait_chain: str,
              partner_chain: str,
              *,
              cutoff: float = CUTOFF_IFACE,
              cache_dir: Optional[Path] = None,
              tm_min: float = TM_MIN,
              perda_max: float = PERDA_MAX,
              strict: bool = True,
              mapear_parceiro: bool = True) -> AirHomologo:
    """AIR para um par, projetado de `template_id` na numeracao de `target_cif`.

    `bait_chain` = a isca no modelo alvo; `partner_chain` = o parceiro.
    A numeracao devolvida e `res_id` do mmCIF (= auth_seq_id), a MESMA de
    `funnel.interface.iface_residues`, para os dois caminhos serem comparaveis.

    Com `strict=True` (default) levanta ValueError quando o mapeamento nao
    sustenta a alegacao de independencia (TM baixo ou perda alta). Falhar aqui e
    o comportamento correto: um AIR ruidoso produz o mesmo fnat=0 de uma
    interface errada, e os dois casos ficariam indistinguiveis.
    """
    if template_id.upper() not in TEMPLATES:
        raise KeyError(f"[air_homolog] template desconhecido: {template_id}. "
                       f"Conhecidos: {sorted(TEMPLATES)}")
    meta = TEMPLATES[template_id.upper()]
    cam = buscar_template(template_id, cache_dir)

    gt_tpl = _cadeia_proteica(cam, meta["bait"])
    pt_tpl = _cadeia_proteica(cam, meta["partner"])
    alvo = Path(target_cif)
    gt_tgt = _cadeia_proteica(alvo, bait_chain)
    pt_tgt = _cadeia_proteica(alvo, partner_chain)

    if_g_tpl, if_p_tpl = _interface_template(gt_tpl, pt_tpl, cutoff)
    if not if_g_tpl:
        raise RuntimeError(
            f"[air_homolog] {template_id}: nenhum contato < {cutoff} A entre as "
            f"cadeias {meta['bait']} e {meta['partner']} -- confira o catalogo")

    mapa_g, m_g = _mapear(gt_tpl, gt_tgt, f"{template_id}:isca")

    bait = sorted({mapa_g[r] for r in if_g_tpl if r in mapa_g})
    nao_map = sorted(r for r in if_g_tpl if r not in mapa_g)
    cobertura = 1.0 - (len(nao_map) / len(if_g_tpl))

    avisos: List[str] = []
    partner: List[int] = []
    tm_p = cob_p = 0.0
    if mapear_parceiro:
        try:
            mapa_p, m_p = _mapear(pt_tpl, pt_tgt, f"{template_id}:parceiro")
            partner = sorted({mapa_p[r] for r in if_p_tpl if r in mapa_p})
            tm_p = m_p["tm_score"]
            cob_p = (len(partner) / len(if_p_tpl)) if if_p_tpl else 0.0
            if tm_p < tm_min:
                avisos.append(
                    f"parceiro com TM-score {tm_p:.2f} < {tm_min}: o lado do "
                    f"parceiro do AIR e pouco confiavel; considere AIR "
                    f"assimetrico (so a isca ativa, parceiro passivo)")
        except Exception as exc:                 # noqa: BLE001
            avisos.append(f"mapeamento do parceiro falhou ({exc}); AIR sai "
                          f"assimetrico, so com o lado da isca")

    if m_g["insercoes"]:
        avisos.append(
            "o alvo tem insercoes ausentes no template "
            + ", ".join(f"{a}-{b}" for a, b in m_g["insercoes"])
            + "; o homologo nao informa sobre esses residuos, ainda que caiam na "
              "interface")

    air = AirHomologo(
        template=template_id.upper(),
        template_descricao=meta["descricao"],
        bait_residues=bait,
        partner_residues=partner,
        tm_score=m_g["tm_score"],
        rmsd=m_g["rmsd"],
        n_alinhados=m_g["n_alinhados"],
        identidade_seq=m_g["identidade_seq"],
        cobertura=round(cobertura, 4),
        residuos_nao_mapeados=nao_map,
        tm_score_parceiro=tm_p,
        cobertura_parceiro=round(cob_p, 4),
        insercoes_alvo=m_g["insercoes"],
        avisos=avisos,
    )

    if strict:
        if air.tm_score < tm_min:
            raise ValueError(
                f"[air_homolog] {template_id}: TM-score da isca "
                f"{air.tm_score:.3f} < {tm_min}. O mapeamento nao sustenta um AIR "
                f"independente -- com este ruido, um fnat=0 no docking nao "
                f"distinguiria 'interface errada' de 'restricao sem sentido'.")
        if cobertura < (1.0 - perda_max):
            raise ValueError(
                f"[air_homolog] {template_id}: so {cobertura:.0%} da interface do "
                f"template mapeou no alvo (minimo {1.0 - perda_max:.0%}); "
                f"{len(nao_map)} residuos sem correspondencia: {nao_map}")
        if not air.bait_residues:
            raise ValueError(f"[air_homolog] {template_id}: AIR vazio no lado da "
                             f"isca apos a projecao")
    return air


def aplicar_ao_manifesto(manifest, air: AirHomologo, *, registrar=True):
    """Sobrescreve os AIR do `PairManifest` com o AIR homologo.

    Mexe SO nas duas listas que `haddock_adapter.build_request` consome
    (`active_residues` = isca, `partner_active_residues` = parceiro), deixando
    motor, referencia do caprieval e protocolo intactos -- assim a UNICA variavel
    entre o caminho circular e este e a ORIGEM da restricao, que e o que torna a
    comparacao controlada.

    `PairManifest.from_dict` ignora campos desconhecidos, mas `to_dict` e escrito
    a mao: a proveniencia vai para um arquivo ao lado, nao para o manifesto.
    """
    manifest.active_residues = list(air.bait_residues)
    manifest.partner_active_residues = list(air.partner_residues or [])
    # Passivos vao para campo PROPRIO: grava-los como ativos torna a restricao
    # insatisfazivel (exigiria a superficie inteira do parceiro na interface).
    #
    # A atribuicao acima e INCONDICIONAL de proposito. Um `if
    # air.partner_residues:` aqui parece inofensivo e nao e: quando o AIR
    # homologo nao tem lado do parceiro, o `if` deixa INTACTOS os ativos que o
    # `ensure_split` preencheu com os contatos do PROPRIO modelo Boltz -- ou
    # seja, reintroduz pela porta de tras exatamente o caminho circular que
    # este modulo existe para evitar, e o ambig.tbl sai com ativos de parceiro
    # que ninguem pediu.
    manifest.partner_passive_residues = list(getattr(air, "partner_passive", []) or [])
    if registrar:
        for k, v in (("air_source", "homolog"), ("air_provenance", air.to_dict())):
            try:
                setattr(manifest, k, v)
            except Exception:                    # noqa: BLE001
                pass
    return manifest


def salvar_proveniencia(air: AirHomologo, destino: "str|Path") -> Path:
    """Grava a proveniencia em JSON ao lado do run -- e o que permite auditar
    depois de onde vieram as restricoes."""
    p = Path(destino)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(air.to_dict(), indent=2, ensure_ascii=False),
                 encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# Passivos de superficie
# --------------------------------------------------------------------------- #
def passivos_superficie(target_cif: "str|Path", chain: str, *,
                        sasa_min: float = 0.20, max_res: int = 400) -> List[int]:
    """Residuos EXPOSTOS da cadeia -- os 'passivos' do AIR do HADDOCK.

    Serve ao caso em que o homologo informa um lado da interface mas nao o outro:
    quando o parceiro do template e o do alvo nao sao homologos de verdade, o
    TM-score do lado do parceiro desaba e projetar a interface dele seria
    inventar dado. Declarando o parceiro
    como passivo, exige-se que ele participe da interface por ALGUM residuo
    exposto, sem afirmar quais -- o que prende a pose no refinamento sem forjar
    informacao que nao existe.

    `sasa_min` e a fracao de area acessivel relativa ao maximo do tipo de residuo.
    """
    import numpy as np
    struc, pdbx = _struc()
    arr = pdbx.get_structure(pdbx.CIFFile.read(str(target_cif)), model=1)
    sub = arr[(arr.chain_id == chain) & struc.filter_amino_acids(arr)]
    if sub.array_length() == 0:
        raise ValueError(f"[air_homolog] cadeia '{chain}' vazia em {target_cif}")
    # SASA por atomo -> soma por residuo -> normaliza pelo maximo observado do
    # tipo de residuo neste proprio calculo (evita depender de tabela externa).
    sasa = struc.sasa(sub, vdw_radii="Single")
    sasa = np.nan_to_num(sasa, nan=0.0)
    por_res: Dict[int, float] = {}
    nome_res: Dict[int, str] = {}
    for i in range(sub.array_length()):
        r = int(sub.res_id[i])
        por_res[r] = por_res.get(r, 0.0) + float(sasa[i])
        nome_res[r] = sub.res_name[i]
    maxpor: Dict[str, float] = {}
    for r, v in por_res.items():
        n = nome_res[r]
        maxpor[n] = max(maxpor.get(n, 0.0), v)
    expostos = [r for r, v in sorted(por_res.items())
                if maxpor.get(nome_res[r], 1.0) > 0
                and v / maxpor[nome_res[r]] >= sasa_min]
    if len(expostos) > max_res:
        # mantem os mais expostos -- uma lista gigante de passivos equivale a
        # nenhuma restricao e so encarece o docking
        expostos = sorted(sorted(expostos, key=lambda r: -por_res[r])[:max_res])
    return expostos


def com_parceiro_passivo(air: "AirHomologo", target_cif: "str|Path",
                         partner_chain: str, **kw) -> "AirHomologo":
    """Declara o parceiro como PASSIVO -- em `partner_passive`, nunca em ativos.

    POR QUE O CAMPO SEPARADO IMPORTA: gravar esses residuos como ATIVOS faz o
    ambig.tbl pedir um `assign` por residuo exposto do parceiro, isto e, exigir
    que a superficie INTEIRA dele esteja na interface. A restricao fica
    insatisfazivel, a energia de AIR explode (ordens de grandeza acima do
    normal), o score do HADDOCK fica positivo, e o `fnat` perto de zero que
    aparece no fim e ARTEFATO da restricao impossivel -- nao resultado sobre a
    interface. Se voce ver energia de AIR fora de escala, suspeite disto antes
    de concluir qualquer coisa sobre o par.
    """
    pas = passivos_superficie(target_cif, partner_chain, **kw)
    air.partner_residues = []          # nenhum ATIVO do lado do parceiro
    air.partner_passive = pas          # so alvos possiveis
    air.avisos.append(
        f"lado do parceiro = {len(pas)} residuos PASSIVOS de superficie (TM do "
        f"parceiro {air.tm_score_parceiro:.3f} baixo demais para projetar a "
        f"interface do homologo). Passivos NAO geram assign proprio.")
    air.fonte = "homologo_cristalografico+passivos"
    return air


# --------------------------------------------------------------------------- #
# Controles negativos -- sem eles nenhum resultado de AIR homologo vale nada
# --------------------------------------------------------------------------- #
def air_embaralhado(air: "AirHomologo", target_cif: "str|Path", bait_chain: str,
                    partner_chain: str = "A", *, seed: int = 0,
                    dist_min: float = 15.0, sasa_min: float = 0.20,
                    cutoff_iface: float = CUTOFF_IFACE) -> "AirHomologo":
    """CONTROLE NEGATIVO: mesmo NUMERO de restricoes, em residuos EXPOSTOS e
    ESPACIALMENTE distantes da interface de REFERENCIA. Tem de dar fnat ~ 0.

    ARMADILHA QUE ESTA FUNCAO JA CAIU: sortear por distancia de SEQUENCIA (ex.:
    >= 8 posicoes de distancia dos residuos do AIR) NAO produz um controle. Numa
    proteina dobrada, residuos distantes na sequencia sao vizinhos no espaco, e
    o sorteio sai indistinguivel de uniforme: boa parte dos residuos escolhidos
    cai DENTRO da interface de referencia, e outros tantos estao enterrados
    apesar de o criterio dizer "superficie". Um controle assim nao controla
    nada, e a afirmacao "restricoes fora da interface deram fnat 0" seria
    falsa. O seed tambem tem de ser registrado, ou o controle nao e reprodutivel.

    O criterio agora e ESPACIAL e contra a referencia certa:
      1. calcula a interface REAL do complexo alvo (contatos < cutoff);
      2. exige distancia minima `dist_min` (A) do centroide de cada candidato
         aos atomos dessa interface;
      3. exige exposicao ao solvente (SASA relativa >= sasa_min);
      4. registra o seed em `seed_controle`, para a lista ser reproduzivel.
    """
    import random
    import numpy as np
    struc, pdbx = _struc()

    alvo = Path(target_cif)
    arr = pdbx.get_structure(pdbx.CIFFile.read(str(alvo)), model=1)
    bait = arr[(arr.chain_id == bait_chain) & struc.filter_amino_acids(arr)]
    parc = arr[(arr.chain_id == partner_chain) & struc.filter_amino_acids(arr)]
    if bait.array_length() == 0 or parc.array_length() == 0:
        raise ValueError("[air_homolog] cadeia vazia ao montar o controle")

    # 1) interface REAL do alvo (a mesma que o caprieval usa como referencia)
    cell = struc.CellList(parc, cell_size=cutoff_iface)
    iface = set()
    for i in range(bait.array_length()):
        if [j for j in cell.get_atoms(bait.coord[i], radius=cutoff_iface) if j >= 0]:
            iface.add(int(bait.res_id[i]))
    if not iface:
        raise RuntimeError("[air_homolog] alvo sem interface: controle sem sentido")
    coord_if = bait.coord[np.isin(bait.res_id, list(iface))]

    # 2) exposicao
    sasa = np.nan_to_num(struc.sasa(bait, vdw_radii="Single"), nan=0.0)
    por_res, nome_res = {}, {}
    for i in range(bait.array_length()):
        r = int(bait.res_id[i])
        por_res[r] = por_res.get(r, 0.0) + float(sasa[i])
        nome_res[r] = bait.res_name[i]
    maxpor = {}
    for r, v in por_res.items():
        maxpor[nome_res[r]] = max(maxpor.get(nome_res[r], 0.0), v)

    # 3) candidatos: expostos E longe da interface
    cands, diag = [], []
    for r in sorted(set(int(x) for x in bait.res_id)):
        if r in iface:
            continue
        mx = maxpor.get(nome_res[r], 0.0)
        if mx <= 0 or (por_res[r] / mx) < sasa_min:
            continue                                   # enterrado
        cr = bait.coord[bait.res_id == r]
        d = float(np.sqrt(((cr[:, None, :] - coord_if[None, :, :]) ** 2)
                          .sum(-1)).min())
        if d >= dist_min:
            cands.append(r)
            diag.append(d)

    n = len(air.bait_residues)
    if len(cands) < n:
        raise RuntimeError(
            f"[air_homolog] so {len(cands)} residuos expostos a >= {dist_min} A da "
            f"interface, preciso de {n}. Baixe dist_min ou reduza o AIR -- NAO "
            f"aceite um controle mais fraco em silencio.")
    rnd = random.Random(seed)
    sorteados = sorted(rnd.sample(cands, n))
    dmin = min(diag[cands.index(r)] for r in sorteados)

    return AirHomologo(
        template=f"{air.template}-EMBARALHADO",
        template_descricao=("controle negativo: residuos expostos e a >= "
                            f"{dist_min} A da interface de referencia"),
        bait_residues=sorteados,
        partner_residues=[],
        partner_passive=list(air.partner_passive or air.partner_residues or []),
        seed_controle=seed,
        fonte="controle_embaralhado_espacial",
        avisos=[f"CONTROLE: {n} ativos, todos expostos (SASA rel >= {sasa_min}) e "
                f"a >= {dmin:.1f} A da interface de referencia; parceiro IDENTICO "
                f"ao run positivo (controle pareado); seed={seed}. Espera-se fnat "
                f"~ 0 -- qualquer convergencia aqui invalida a fase inteira."],
    )



# --------------------------------------------------------------------------- #
# Randomizacao da pose de partida
# --------------------------------------------------------------------------- #
def randomizar_ligante(ligand_pdb: "str|Path", *, seed: int = 0,
                       separacao: float = 25.0, saida=None) -> Path:
    """Afasta e reorienta o ligante ANTES do docking. Devolve o caminho gravado.

    POR QUE ISTO E OBRIGATORIO
    --------------------------
    `split_complex` recorta receptor e ligante do complexo PRESERVANDO as
    coordenadas originais: o RMSD do recorte contra a referencia e zero, por
    construcao. Com as duas cadeias JA na pose correta, o docking pode
    simplesmente nao se mover, e o caprieval registra DockQ ~1 / i-RMSD ~0 --
    a pose de PARTIDA avaliada contra si mesma, nao um acerto do docking. E o
    mesmo vies da circularidade, agora pela geometria em vez das restricoes, e
    ele contamina retroativamente qualquer run que tenha partido de cadeias
    recortadas sem randomizacao.

    Afastando o ligante e girando-o, o docking passa a ter de RECONSTRUIR a
    pose -- que e a unica versao da pergunta que vale a pena responder.
    """
    import numpy as np
    p = Path(ligand_pdb)
    dest = Path(saida) if saida else p

    linhas = p.read_text(encoding="utf-8", errors="replace").splitlines()
    idx, coords = [], []
    for i, ln in enumerate(linhas):
        if ln.startswith(("ATOM", "HETATM")):
            idx.append(i)
            coords.append([float(ln[30:38]), float(ln[38:46]), float(ln[46:54])])
    if not idx:
        raise ValueError(f"[air_homolog] sem atomos em {p}")
    X = np.array(coords)

    rng = np.random.default_rng(seed)
    # rotacao uniforme por quaternion aleatorio
    q = rng.normal(size=4)
    q /= np.linalg.norm(q)
    w, x, y, z = q
    R = np.array([
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    cen = X.mean(axis=0)
    Xr = (X - cen) @ R.T + cen
    # afasta numa direcao aleatoria
    d = rng.normal(size=3)
    d /= np.linalg.norm(d)
    Xr = Xr + d * separacao

    saida_linhas = list(linhas)
    for k, i in enumerate(idx):
        ln = linhas[i]
        saida_linhas[i] = (f"{ln[:30]}{Xr[k,0]:8.3f}{Xr[k,1]:8.3f}"
                           f"{Xr[k,2]:8.3f}{ln[54:]}")
    dest.write_text("\n".join(saida_linhas) + "\n", encoding="utf-8")
    return dest
