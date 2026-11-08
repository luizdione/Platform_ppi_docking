# -*- coding: utf-8 -*-
"""Analise de MD STREAMING (memoria limitada): RMSD(t) + RMSF por residuo +
frame representativo. NAO usa in_memory (que estouraria a RAM com trajetorias
grandes). Le frame-a-frame; RMSF por Welford sobre CA alinhados (Kabsch).

Gera, por run, em ``<saida>/<nome>/``:
  rmsd_vs_tempo.{png,pdf,csv}, rmsf_por_residuo.{png,pdf,csv}, repr_frame.pdb
e atualiza md_summary.json com rmsd_media_A/rmsd_final_A/estavel.

Uso::

    python tools/md_streaming_analysis.py <nome> <topologia> <trajetoria> \
        [--ns-total 10.0] [--stride 1] [--saida <pasta>]

`--saida` cai, na ausencia, em ``$PPI_DOCK_DATA_ROOT/_md_analysis`` e, sem essa
env, em ``./_md_analysis``. NENHUM caminho de trajetoria vem embutido no
codigo: a MD e dado do usuario, e fica fora deste repositorio.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import MDAnalysis as mda
from MDAnalysis.analysis import align, rms

def staging(explicito: str = "") -> Path:
    """Pasta de saida das analises. Sem default de disco (ver docstring)."""
    if explicito:
        return Path(explicito)
    raiz = os.environ.get("PPI_DOCK_DATA_ROOT", "").strip()
    return (Path(raiz) if raiz else Path.cwd()) / "_md_analysis"

REPR_SEL = "protein or resname ATP ADP MG"


def _plot(x, y, xl_pt, xl_en, yl_pt, yl_en, titulo_pt, titulo_en, out_base, csvcols):
    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=150)
    ax.plot(x, y, lw=1.1, color="#0e7490")
    ax.set_xlabel(f"{xl_pt}  /  {xl_en}")
    ax.set_ylabel(f"{yl_pt}  /  {yl_en}")
    ax.set_title(f"{titulo_pt}\n{titulo_en}", fontsize=10)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(f"{out_base}.png"); fig.savefig(f"{out_base}.pdf")
    plt.close(fig)
    with open(f"{out_base}.csv", "w", encoding="utf-8") as fh:
        fh.write(",".join(csvcols) + "\n")
        for a, b in zip(x, y):
            fh.write(f"{a},{b}\n")


def analisar(nome, cfg, saida: "Path | None" = None):
    top, dcd, stride = cfg["top"], cfg["dcd"], cfg["stride"]
    out = (saida or staging()) / nome
    out.mkdir(parents=True, exist_ok=True)
    if not Path(top).is_file() or not Path(dcd).is_file():
        print(f"[{nome}] ARQUIVO AUSENTE (top={Path(top).is_file()} dcd={Path(dcd).is_file()}) - pulado")
        return None

    u = mda.Universe(top, dcd)
    n_frames = len(u.trajectory)
    print(f"[{nome}] atomos={len(u.atoms)} frames={n_frames} stride={stride}", flush=True)

    # ---- RMSD backbone vs tempo (streaming) ----
    bb = "backbone"
    R = rms.RMSD(u, select=bb, ref_frame=0)
    R.run(step=stride)
    res = R.results.rmsd  # colunas: frame, time, rmsd
    frames_idx = res[:, 0]
    rmsd = res[:, 3] if res.shape[1] > 3 else res[:, 2]
    tempo_ns = frames_idx / max(n_frames - 1, 1) * cfg["ns_total"]
    _plot(tempo_ns, rmsd, "Tempo (ns)", "Time (ns)", "RMSD do backbone (A)", "Backbone RMSD (A)",
          f"RMSD vs tempo - {nome}", f"RMSD over time - {nome}", str(out / "rmsd_vs_tempo"),
          ["tempo_ns", "rmsd_A"])
    rmsd_media = float(np.mean(rmsd)); rmsd_final = float(rmsd[-1])
    # plato: desvio-padrao da 2a metade pequeno (< 1.5 A) => estabilizou
    meia = rmsd[len(rmsd) // 2:]
    estavel = bool(np.std(meia) < 1.5)
    print(f"[{nome}] RMSD media={rmsd_media:.2f} final={rmsd_final:.2f} std_2a_metade={np.std(meia):.2f} estavel={estavel}", flush=True)

    # ---- RMSF por CA (streaming Welford, alinhado por Kabsch) ----
    try:
        ca = u.select_atoms("protein and name CA")
        ref = u.select_atoms("protein and name CA")
        u.trajectory[0]
        ref_pos = ref.positions.copy()
        ref_cog = ref_pos.mean(axis=0)
        ref_c = ref_pos - ref_cog
        n = ca.n_atoms
        mean = np.zeros((n, 3)); M2 = np.zeros((n, 3)); cnt = 0
        for ts in u.trajectory[::stride]:
            mob = ca.positions - ca.positions.mean(axis=0)
            Rm, _ = align.rotation_matrix(mob, ref_c)
            aligned = mob @ Rm.T
            cnt += 1
            d = aligned - mean
            mean += d / cnt
            M2 += d * (aligned - mean)
        var = M2 / max(cnt, 1)
        rmsf = np.sqrt(var.sum(axis=1))
        resids = ca.resids
        _plot(resids, rmsf, "Residuo (CA)", "Residue (CA)", "RMSF (A)", "RMSF (A)",
              f"RMSF por residuo - {nome}", f"Per-residue RMSF - {nome}", str(out / "rmsf_por_residuo"),
              ["resid", "rmsf_A"])
        print(f"[{nome}] RMSF: {n} CA, media={float(np.mean(rmsf)):.2f} A", flush=True)
    except Exception as exc:
        print(f"[{nome}] RMSF falhou (nao-bloqueante): {type(exc).__name__}: {exc}", flush=True)

    # ---- frame representativo (ultimo, so proteina+cofatores) ----
    try:
        u.trajectory[-1]
        sel = u.select_atoms(REPR_SEL)
        pdb = out / "repr_frame.pdb"
        sel.write(str(pdb))
        print(f"[{nome}] frame representativo: {sel.n_atoms} atomos -> {pdb.name} ({pdb.stat().st_size//1024} KB)", flush=True)
    except Exception as exc:
        print(f"[{nome}] frame repr falhou: {type(exc).__name__}: {exc}", flush=True)

    # ---- atualizar md_summary.json ----
    js = out / "md_summary.json"
    summ = json.loads(js.read_text(encoding="utf-8")) if js.is_file() else {}
    summ.update({"rmsd_media_A": round(rmsd_media, 2), "rmsd_final_A": round(rmsd_final, 2),
                 "rmsd_estavel": estavel, "rmsd_n_frames": int(len(rmsd))})
    js.write_text(json.dumps(summ, ensure_ascii=False, indent=2), encoding="utf-8")
    return summ


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="md_streaming_analysis",
        description="RMSD(t) + RMSF por residuo + frame representativo, em streaming",
    )
    p.add_argument("nome", help="rotulo da run (nome da subpasta de saida)")
    p.add_argument("topologia", help=".pdb/.gro/.psf da MD")
    p.add_argument("trajetoria", help=".dcd/.xtc da MD")
    p.add_argument("--ns-total", type=float, required=True,
                   help="duracao total da trajetoria, em ns (eixo do tempo)")
    p.add_argument("--stride", type=int, default=1)
    p.add_argument("--saida", default="",
                   help="pasta de saida (default: $PPI_DOCK_DATA_ROOT/_md_analysis)")
    args = p.parse_args(argv)

    cfg = dict(top=args.topologia, dcd=args.trajetoria,
               stride=args.stride, ns_total=args.ns_total)
    try:
        analisar(args.nome, cfg, staging(args.saida))
    except Exception as exc:  # noqa: BLE001
        print(f"[{args.nome}] ERRO: {type(exc).__name__}: {exc}")
        return 1
    print("FIM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
