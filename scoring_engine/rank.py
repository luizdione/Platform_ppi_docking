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
"""scoring_engine.rank - Estagio 5: consolida scores em tabela ranqueada e shortlist para co-IP."""
from __future__ import annotations
import csv
from dataclasses import asdict
from pathlib import Path
from . import config


def _ip(sc) -> float:
    """ipTM da interface isca:parceiro quando disponivel; senao o ipTM global."""
    v = getattr(sc, "iface_iptm", None)
    if v is None:
        v = getattr(sc, "iptm", None)
    return v or 0.0


def classify(sc) -> str:
    """HIT/MAYBE/weak. Usa o ipTM de interface (isca:parceiro) + contatos e, quando
    a matriz PAE existe, o PAE de interface como filtro de qualidade. PAE ausente
    NAO penaliza (degrada para ipTM + contatos, como a triagem validada)."""
    ip = _ip(sc)
    pae = sc.pae_iface_mean
    pae_ok = pae is None or pae <= config.SCORE["pae_iface_max"]
    if ip >= config.SCORE["iptm_hit"] and pae_ok and sc.n_contacts >= config.SCORE["min_contacts"]:
        return "HIT"
    if ip >= config.SCORE["iptm_maybe"]:
        return "MAYBE"
    return "weak"


_COLS = ["name", "call", "iface_iptm", "iptm", "ptm", "confidence_score", "ipsae",
         "complex_plddt", "pae_iface_mean", "pae_iface_min", "iface_plddt", "bsa",
         "n_contacts", "n_iface_res"]


def to_table(scores, out_tsv):
    rows = []
    for sc in scores:
        d = asdict(sc)
        d["call"] = classify(sc)
        rows.append(d)
    order = {"HIT": 0, "MAYBE": 1, "weak": 2}
    rows.sort(key=lambda d: (order[d["call"]], -(d.get("iface_iptm") or d.get("iptm") or 0)))
    Path(out_tsv).parent.mkdir(parents=True, exist_ok=True)
    with open(out_tsv, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_COLS, delimiter="\t", extrasaction="ignore")
        w.writeheader()
        for d in rows:
            w.writerow(d)
    return rows


def shortlist(rows, out_md, project=""):
    hits = [r for r in rows if r["call"] == "HIT"]
    maybe = [r for r in rows if r["call"] == "MAYBE"]
    L = [f"# Shortlist para co-IP - {project}", ""]
    L.append(f"Triagem in silico priorizou **{len(hits)} HIT(s)** "
             f"e {len(maybe)} candidato(s) intermediario(s) (MAYBE).")
    L.append("")
    L.append("| Parceiro | ipTM iface | ipSAE | PAE iface (A) | contatos | call |")
    L.append("|---|---|---|---|---|---|")
    for r in hits + maybe:
        ip = r.get("iface_iptm") if r.get("iface_iptm") is not None else r.get("iptm")
        ip = f"{ip:.2f}" if ip is not None else "-"
        sa = f"{r['ipsae']:.2f}" if r.get("ipsae") is not None else "-"
        pae = f"{r['pae_iface_mean']:.1f}" if r.get("pae_iface_mean") is not None else "-"
        L.append(f"| {r['name']} | {ip} | {sa} | {pae} | {r['n_contacts']} | {r['call']} |")
    Path(out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(out_md).write_text("\n".join(L) + "\n", encoding="utf-8")
    return hits
