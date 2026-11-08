# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# PROVENANCE / PROCEDENCIA
#
# This module was copied verbatim (apart from the import paths, this header and
# the sanitisation of one internal comment) from the author's own MIT-licensed
# package `ppi` (ppi_platform), by Luiz Dione Barbosa De-Melo (IFRJ).
# Only the modules the funnel actually imports were vendored here, so that this
# repository runs without the private package installed. See README, section
# "Vendored scoring engine".
# ---------------------------------------------------------------------------
"""
scoring_engine.score - Estagio 4: avalia complexos preditos pelo Boltz-2.

Metricas por complexo (convencao: isca = cadeias iniciais; PARCEIRO = ULTIMA cadeia):
  ipTM global, ipTM da interface isca:parceiro (pair_chains_iptm), pTM,
  confidence_score, ipSAE (interface robusta a dominios moveis - Dunbrack 2025),
  pLDDT do complexo, PAE medio/min de interface (lido do pae_*.npz), pLDDT de
  interface, BSA (Shrake-Rupley), n. de contatos pesados < 5 A e n. de residuos.

Le a saida REAL do Boltz-2:
  - confidence_*.json  -> chaves confidence_score / ptm / iptm / complex_plddt /
                          pair_chains_iptm  (NAO as chaves AF3 ranking_score/has_clash);
  - pae_*.npz          -> matriz PAE (Angstrom) para PAE-iface + ipSAE.
Buscas de interface e leitura de npz vem de scoring_engine._common. Requer biopython + numpy.
"""
from __future__ import annotations
import os
import glob
import json
import zipfile
import tempfile
from dataclasses import dataclass, field
import numpy as np

from . import _common
from .ipsae import ipsae


def _find(folder, *pats):
    for p in pats:
        hits = sorted(glob.glob(os.path.join(folder, "**", p), recursive=True))
        if hits:
            return hits
    return []


def _materialize(case):
    if os.path.isdir(case):
        return case
    if str(case).lower().endswith(".zip"):
        tmp = tempfile.mkdtemp(prefix="ppi_")
        with zipfile.ZipFile(case) as z:
            z.extractall(tmp)
        return tmp
    raise ValueError(case)


def _summary(folder):
    """Melhor confidence JSON do Boltz-2 (exclui arquivos de PAE). Ordena por
    confidence_score (chave REAL do Boltz) -> iptm -> ptm."""
    files = _find(folder, "confidence_*.json", "*summary_confidences*.json", "*confidences*.json")
    files = [f for f in files if "pae" not in os.path.basename(f).lower()]
    best = None
    for f in files:
        try:
            js = json.load(open(f, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(js, dict):
            key = js.get("confidence_score") or js.get("iptm") or js.get("ptm") or 0
            if best is None or key > best[0]:
                best = (key, js)
    return best[1] if best else {}


def _pae_matrix(folder):
    """Matriz PAE (NxN, Angstrom) do .npz do Boltz. None se ausente/ilegivel."""
    hits = _find(folder, "pae_*.npz", "*pae*.npz")
    if not hits:
        return None
    try:
        return _common.load_npz(hits[0]).astype(float)
    except Exception:  # noqa: BLE001
        return None


def _iface_iptm(summ, n_chains):
    """max ipTM entre cada cadeia do bait e a cadeia PARCEIRA (ultima), via
    pair_chains_iptm (indices 0-based em string). Fallback: iptm global."""
    pc = summ.get("pair_chains_iptm") or {}
    last = n_chains - 1
    vals = []
    for b in range(last):
        v = (pc.get(str(b)) or {}).get(str(last))
        if v is not None:
            vals.append(float(v))
    return max(vals) if vals else summ.get("iptm")


def _bsa(model):
    from Bio.PDB.SASA import ShrakeRupley
    from Bio.PDB import Model
    import copy
    sr = ShrakeRupley()
    sr.compute(model, level="C")
    comp = sum(c.sasa for c in model)
    iso = 0.0
    for c in list(model):
        m2 = Model.Model(0)
        m2.add(copy.deepcopy(c))
        sr.compute(m2, level="C")
        iso += list(m2)[0].sasa
    return iso - comp


def _plddt_by_res(chain):
    """{resnum: pLDDT medio dos atomos} de uma cadeia (pLDDT gravado no B-factor)."""
    d = {}
    for r in chain:
        if r.id[0] != " ":
            continue
        bs = [a.get_bfactor() for a in r]
        if bs:
            d[r.id[1]] = float(np.mean(bs))
    return d


@dataclass
class Score:
    name: str
    iptm: "float | None" = None
    iface_iptm: "float | None" = None      # ipTM do par isca:parceiro (pair_chains_iptm)
    ptm: "float | None" = None
    confidence_score: "float | None" = None  # chave real do Boltz (era ranking_score/AF3)
    ipsae: "float | None" = None            # interface robusta (Dunbrack 2025)
    complex_plddt: "float | None" = None    # pLDDT global do complexo (0-100)
    pae_iface_mean: "float | None" = None   # PAE inter-cadeia medio (A) - CONSERTADO (le npz)
    pae_iface_min: "float | None" = None
    iface_plddt: "float | None" = None
    bsa: "float | None" = None
    n_contacts: int = 0
    n_iface_res: int = 0


def score_case(case, out_dir=None):
    folder = _materialize(case)
    name = os.path.splitext(os.path.basename(str(case).rstrip("/\\")))[0]
    cifs = _find(folder, "*model_0.cif", "*_model_0.cif", "*.cif")
    if not cifs:
        return None
    model = _common.parse_model(cifs[0])
    chains, per, contacts = _common.interface_pairs(model)
    if len(chains) < 2:
        return None
    sizes = [sum(1 for r in c if r.id[0] == " ") for c in chains]  # ordem das cadeias (A,B,...)

    summ = _summary(folder)

    # PAE de interface + ipSAE a partir da matriz REAL (pae_*.npz). Convencao:
    # bait = todas as cadeias menos a ultima; PARCEIRO = ultima. n1 = tokens do bait.
    pae = _pae_matrix(folder)
    pae_mean = pae_min = ipsae_val = None
    if pae is not None and pae.shape[0] == sum(sizes):
        n1 = sum(sizes[:-1])
        cross = np.concatenate([pae[:n1, n1:].ravel(), pae[n1:, :n1].ravel()])
        if cross.size:
            pae_mean, pae_min = float(cross.mean()), float(cross.min())
        ipsae_val = float(ipsae(pae, n1))

    # pLDDT do complexo (JSON, normalizado 0-100).
    cp = summ.get("complex_plddt")
    if cp is not None and cp <= 1.0:
        cp = cp * 100.0

    # pLDDT dos residuos de interface (B-factor do cif; normaliza p/ 0-100 se fracionario).
    plddt = {}
    for c in chains:
        plddt.update(_plddt_by_res(c))
    iface_res = set().union(*per.values()) if per else set()
    ipl = [plddt[r] for r in iface_res if r in plddt]
    iface_plddt = None
    if ipl:
        iface_plddt = float(np.mean(ipl))
        if iface_plddt <= 1.0:
            iface_plddt *= 100.0

    try:
        bsa = _bsa(model)
    except Exception:  # noqa: BLE001
        bsa = None

    sc = Score(
        name=name,
        iptm=summ.get("iptm"),
        iface_iptm=_iface_iptm(summ, len(chains)),
        ptm=summ.get("ptm"),
        confidence_score=summ.get("confidence_score"),
        ipsae=ipsae_val,
        complex_plddt=cp,
        pae_iface_mean=pae_mean,
        pae_iface_min=pae_min,
        iface_plddt=iface_plddt,
        bsa=bsa,
        n_contacts=len(contacts),
        n_iface_res=sum(len(v) for v in per.values()),
    )
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"{name}_contatos.tsv"), "w", encoding="utf-8") as fh:
            fh.write("c1\tres1\tname1\tc2\tres2\tname2\n")
            for (c1, r1, rn1), (c2, r2, rn2) in sorted(contacts):
                fh.write(f"{c1}\t{r1}\t{rn1}\t{c2}\t{r2}\t{rn2}\n")
    return sc


def score_dir(results_dir, out_dir=None):
    rows = []
    for nome in sorted(os.listdir(results_dir)):
        p = os.path.join(results_dir, nome)
        if os.path.isdir(p) or p.lower().endswith(".zip"):
            try:
                sc = score_case(p, out_dir)
                if sc:
                    rows.append(sc)
            except Exception as e:  # noqa: BLE001
                print(f"ERRO {nome}: {e}")
    return rows


# ---------------------------------------------------------------------------
# Oligomeros (N >= 2 cadeias) - avidez / auto-associacao
#
# score_case() acima assume a convencao "isca = todas as cadeias menos a ultima;
# PARCEIRO = ultima": para N=2 isso e correto, mas para N>=3 mistura pares
# distintos num bloco PAE so (ipSAE/PAE_iface ficam SEM SENTIDO, sem crashar).
# As funcoes abaixo pontuam a interface POR PAR DE CADEIA: reusam a MESMA busca
# de contato (_common.interface_pairs, que ja etiqueta cada contato pelo par de
# cadeia de origem) e a MESMA ipsae() de 2 blocos (inalterada), so fatiando a
# matriz PAE no bloco 2x2 daquele par. Premissa: 1 token PAE = 1 residuo padrao
# (verdadeiro sem ligante/PTM/glicano - coerente com o escopo). score_case() e o
# caminho 1:1 auditado ficam INTOCADOS.
# ---------------------------------------------------------------------------
@dataclass
class PairScore:
    chain_a: str
    chain_b: str
    n_contacts: int = 0
    ipsae: "float | None" = None            # ipSAE do par (bloco PAE 2x2 daquele par)
    pae_iface_mean: "float | None" = None    # PAE inter-cadeia do par (A)
    pae_iface_min: "float | None" = None
    bsa: "float | None" = None               # BSA do par isolado (opcional)


@dataclass
class OligomerScore:
    name: str
    n_chains: int
    chain_ids: list = field(default_factory=list)
    chain_sizes: list = field(default_factory=list)
    iptm: "float | None" = None              # global (protein_iptm/iptm do JSON)
    ptm: "float | None" = None
    confidence_score: "float | None" = None
    complex_plddt: "float | None" = None
    bsa_total: "float | None" = None         # BSA do complexo inteiro (todas cadeias)
    n_contacts_total: int = 0
    n_iface_res_total: int = 0
    pairs: list = field(default_factory=list)  # list[PairScore] (so pares com >=min_contacts)
    ipsae_min: "float | None" = None         # gargalo entre os pares em contato
    ipsae_mean: "float | None" = None


def _chain_token_slices(chains):
    """{chain_id: slice} de tokens PAE na ordem das cadeias do modelo, contando
    so residuos padrao (1 token = 1 residuo; cadeia sem residuo padrao ignorada).
    Retorna (slices, n_tokens_total)."""
    slices, off = {}, 0
    for c in chains:
        n = sum(1 for r in c if r.id[0] == " ")
        if n == 0:
            continue
        slices[c.id] = slice(off, off + n)
        off += n
    return slices, off


def _bsa_pair(model, id_a, id_b):
    """BSA (Shrake-Rupley) apenas do par (a,b), isolando as 2 cadeias do resto do
    complexo (mesma logica de _bsa, restrita a 2 cadeias)."""
    from Bio.PDB.SASA import ShrakeRupley
    from Bio.PDB import Model
    import copy
    m2 = Model.Model(0)
    for c in model:
        if c.id in (id_a, id_b):
            m2.add(copy.deepcopy(c))
    if len(list(m2)) < 2:
        return None
    sr = ShrakeRupley()
    sr.compute(m2, level="C")
    comp = sum(c.sasa for c in m2)
    iso = 0.0
    for c in list(m2):
        m3 = Model.Model(0)
        m3.add(copy.deepcopy(c))
        sr.compute(m3, level="C")
        iso += list(m3)[0].sasa
    return iso - comp


def score_oligomer_model(cif, pae=None, summ=None, name=None, out_dir=None,
                         min_contacts=1, compute_pair_bsa=False, compute_total_bsa=True):
    """Pontua UM modelo oligomerico (>=2 cadeias) a partir de arquivos explicitos.

    cif   : caminho do .cif do modelo.
    pae   : np.ndarray, caminho .npz, ou None (sem ipSAE/PAE_iface).
    summ  : dict de confianca (iptm/ptm/confidence_score/complex_plddt) ou None.
    compute_total_bsa : BSA (Shrake-Rupley) do complexo inteiro - caro em N/residuos
                        grandes; desligar na varredura de muitas amostras.
    Retorna OligomerScore (ou None se <2 cadeias / cif ilegivel).
    """
    summ = summ or {}
    name = name or os.path.splitext(os.path.basename(str(cif).rstrip("/\\")))[0]
    try:
        model = _common.parse_model(cif)
    except Exception:  # noqa: BLE001
        return None
    chains, per, contacts = _common.interface_pairs(model)
    if len(chains) < 2:
        return None

    slices, ntok = _chain_token_slices(chains)
    chain_ids = list(slices.keys())
    chain_sizes = [slices[c].stop - slices[c].start for c in chain_ids]

    if isinstance(pae, str):
        try:
            pae = _common.load_npz(pae).astype(float)
        except Exception:  # noqa: BLE001
            pae = None
    elif pae is not None:
        pae = np.asarray(pae, dtype=float)
    pae_ok = pae is not None and pae.shape[0] == ntok

    # contatos por par de cadeia (etiqueta ja vem da busca UNICA, sem re-buscar)
    pair_nc = {}
    for (c1, _r1, _n1), (c2, _r2, _n2) in contacts:
        if c1 == c2:
            continue
        pair_nc[tuple(sorted((c1, c2)))] = pair_nc.get(tuple(sorted((c1, c2))), 0) + 1

    pairs = []
    for i in range(len(chain_ids)):
        for j in range(i + 1, len(chain_ids)):
            key = tuple(sorted((chain_ids[i], chain_ids[j])))
            nc = pair_nc.get(key, 0)
            if nc < min_contacts:
                continue
            ps = PairScore(chain_a=key[0], chain_b=key[1], n_contacts=nc)
            if pae_ok:
                sa, sb = slices[key[0]], slices[key[1]]
                cross = np.concatenate([pae[sa, sb].ravel(), pae[sb, sa].ravel()])
                if cross.size:
                    ps.pae_iface_mean, ps.pae_iface_min = float(cross.mean()), float(cross.min())
                idx = np.r_[np.arange(sa.start, sa.stop), np.arange(sb.start, sb.stop)]
                sub = pae[np.ix_(idx, idx)]
                ps.ipsae = float(ipsae(sub, sa.stop - sa.start))
            if compute_pair_bsa:
                try:
                    ps.bsa = _bsa_pair(model, key[0], key[1])
                except Exception:  # noqa: BLE001
                    ps.bsa = None
            pairs.append(ps)

    cp = summ.get("complex_plddt")
    if cp is not None and cp <= 1.0:
        cp = cp * 100.0
    bsa_total = None
    if compute_total_bsa:
        try:
            bsa_total = _bsa(model)
        except Exception:  # noqa: BLE001
            bsa_total = None

    ivals = [p.ipsae for p in pairs if p.ipsae is not None]
    sc = OligomerScore(
        name=name,
        n_chains=len(chain_ids),
        chain_ids=chain_ids,
        chain_sizes=chain_sizes,
        iptm=summ.get("protein_iptm", summ.get("iptm")),
        ptm=summ.get("ptm"),
        confidence_score=summ.get("confidence_score"),
        complex_plddt=cp,
        bsa_total=bsa_total,
        n_contacts_total=len(contacts),
        n_iface_res_total=sum(len(v) for v in per.values()),
        pairs=pairs,
        ipsae_min=min(ivals) if ivals else None,
        ipsae_mean=float(np.mean(ivals)) if ivals else None,
    )
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"{name}_pares.tsv"), "w", encoding="utf-8") as fh:
            fh.write("chain_a\tchain_b\tn_contacts\tipsae\tpae_iface_mean\tpae_iface_min\tbsa\n")
            for p in pairs:
                fh.write(f"{p.chain_a}\t{p.chain_b}\t{p.n_contacts}\t"
                         f"{p.ipsae}\t{p.pae_iface_mean}\t{p.pae_iface_min}\t{p.bsa}\n")
    return sc


def score_oligomer_case(case, out_dir=None, min_contacts=1, compute_pair_bsa=True):
    """Versao folder-based: materializa `case` (dir/zip), acha o *model_0.cif +
    pae_*.npz + confidence_*.json (via _summary) e chama score_oligomer_model.
    Espelha score_case() para o MELHOR modelo ja no layout boltz_results_<tag>."""
    folder = _materialize(case)
    name = os.path.splitext(os.path.basename(str(case).rstrip("/\\")))[0]
    cifs = _find(folder, "*model_0.cif", "*_model_0.cif", "*.cif")
    if not cifs:
        return None
    return score_oligomer_model(cifs[0], _pae_matrix(folder), _summary(folder),
                                name=name, out_dir=out_dir,
                                min_contacts=min_contacts, compute_pair_bsa=compute_pair_bsa)


def score_oligomer_dir(results_dir, out_dir=None):
    rows = []
    for nome in sorted(os.listdir(results_dir)):
        p = os.path.join(results_dir, nome)
        if os.path.isdir(p) or p.lower().endswith(".zip"):
            try:
                sc = score_oligomer_case(p, out_dir)
                if sc:
                    rows.append(sc)
            except Exception as e:  # noqa: BLE001
                print(f"ERRO {nome}: {e}")
    return rows
