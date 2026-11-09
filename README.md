# Platform_ppi_docking

A confidence funnel that turns an **already-computed AI prediction of a
protein–protein complex** into a **refined pair, declared ready for molecular
dynamics** — and that says *no* out loud, with reasons, for the pairs that do
not deserve the compute.

```
    Boltz-2 (already run, elsewhere)
            |
            v
    [ scoring ]      ipTM · interface ipTM · ipSAE · interface PAE
            |        interface pLDDT · BSA · heavy-atom contacts
            v
    [ GATE 1 ]  -----> rejected, with the reason written down
            |
            v
    [ split + interface ]   complex .cif -> receptor.pdb + ligand.pdb
            |               interface residues -> ambiguous restraints (AIR)
            v
    [ HADDOCK3 ]     flexible refinement (local binary or container)
            |
            v
    [ GATE 2 ]  -----> rejected, with the reason written down
            |          DockQ / i-RMSD from caprieval
            v
    pair ready for molecular dynamics (run elsewhere)
```

This repository is the **thin layer in the middle**. It does not predict
structures and it does not run molecular dynamics: those sit upstream and
downstream of it, in their own tools. What lives here is the data contract, the
two gates, the file surgery between the two engines, and the orchestration.

> **This repository contains no research data and no results.** No structure, no
> trajectory, no score table, no prediction output. Every path to data is
> supplied by you through an environment variable, and the funnel only ever
> *reads* what you point it at. The example campaign shipped in `config/` is a
> public reference complex with placeholder paths — see
> [The public example](#the-public-example).

---

## Why a funnel, and why two gates

Predicting a complex is now cheap; refining and simulating one is not. A
flexible HADDOCK3 run costs hours, and a molecular dynamics run costs days. So
the expensive stages must be defended by an explicit, auditable decision — not
by eyeballing a score.

The two gates encode that decision, and they obey three rules:

1. **Never degrade silently.** An unknown threshold in the YAML, a malformed
   CAPRI table or an unreadable PAE matrix raises an error. There is no
   "plausible but wrong" verdict.
2. **Never approve by omission.** A missing decisive metric *rejects*, and the
   reason says `MISSING` explicitly. An empty spreadsheet cell becomes `None`,
   never `0.0` — "not measured" and "measured and bad" are different facts.
3. **Every verdict is auditable.** A `GateResult` carries the reason for *each*
   criterion evaluated, with the observed value, plus the thresholds actually
   used.

### GATE 1 — after the prediction

Decisive, all three required:

| Criterion | Default | What it is |
|---|---|---|
| `ipsae_min` | `0.15` | ipSAE (Dunbrack *et al.*, 2025): pTM restricted to inter-chain pairs whose PAE is below a cutoff. It penalises a small or badly resolved interface, which a global ipTM masks. |
| `coverage_core_face_min` | `0.50` | Fraction of the bait's interface residues falling inside the sub-region you declared as the legitimate binding face. Below this, the interface is smeared over an unproductive face. |
| `require_canonical_face` | `true` | The face label must be in `faces_aceitas`. With no list, the generic rule is "the label contains `canonica`". |

Corroborating — these reject when **present and out of range**, and when
**absent** they are only recorded (unless `strict_missing: true`):

| Criterion | Default |
|---|---|
| `iface_iptm_min` | `0.50` |
| `pae_iface_max` | `10.0` Å |
| `min_contacts` | `10` heavy-atom contacts < 5 Å |

A note on honesty: `n_contacts` is an `int` with no `None` in the contract, and
a complex with a real interface never has zero contacts — so **0 is read as
"not measured"**, not as zero contacts, and falls under the missing-data rule.

### GATE 2 — after the refinement

Reads the `capri_clt.tsv` that HADDOCK3's `caprieval` writes, per cluster:

| Criterion | Default | What it is |
|---|---|---|
| `dockq_min` | `0.23` | The CAPRI "acceptable" boundary (Basu & Wallner, *PLoS ONE* 2016): < 0.23 incorrect; 0.23–0.49 acceptable; 0.49–0.80 medium; ≥ 0.80 high. |
| `irmsd_max` | `4.0` Å | The same "acceptable" boundary under the classic CAPRI criterion. |
| `require_both_capri` | `false` | With `true`, demands DockQ **and** i-RMSD together. |

By default a pair passes on DockQ **or** i-RMSD: they are two expressions of the
same frontier. No caprieval table means no verdict — the pair stays pending with
an explicit reason, never approved by omission.

**All of these are screening thresholds, not universal constants.** They came
from a round of manual curation that preceded the funnel, and the whole point of
`config/funnel.yaml` being the single source of the numbers is that you
recalibrate them for your system. Each threshold there carries a `texto:` field
explaining what it means; the panel's Help tab renders it.

---

## What each piece does

| Path | Role |
|---|---|
| `funnel/contract.py` | The data contract: `PairManifest`, `Scores`, `GateResult`, `HaddockArtifacts`, and the stage machine. Everything else speaks this. |
| `funnel/gate.py` | The two gates, the YAML threshold loader, and `read_capri_clt`. |
| `funnel/scoring.py` | Builds a `PairManifest` from what exists on disk: the score spreadsheet plus the run's confidence JSON. |
| `funnel/interface.py` | Inter-chain interface residues → the active residues that become AIRs. |
| `funnel/split.py` | Complex `.cif` → `receptor.pdb` + `ligand.pdb` + the caprieval reference PDB, with the chain/segid contract the restraints depend on. |
| `funnel/haddock_adapter.py` | Bridge to `engines.haddock3`: builds the request, the `.cfg` and the `ambig.tbl`. |
| `funnel/haddock_docker.py` | Runs HADDOCK3 in a Linux container and translates host paths into the mount. |
| `funnel/air_homolog.py` | **Non-circular restraints**: AIRs projected from a *homologous crystal complex* instead of from the model being tested. See below. |
| `funnel/pipeline.py` | `run_funnel(campaign, ate=...)`: chains the whole thing for a campaign. |
| `core/data.py` | Data layer with no UI: campaign discovery, live scoring, the funnel table, figures. |
| `bridge/` | HTTP API over the funnel (`/score`, `/gate`, `/haddock/prepare|run`, `/funnel/run`, job registry). |
| `web/` | FastAPI panel: pages, job queue with resource locks, 3D viewer. |
| `scoring_engine/` | The vendored scoring engine — see [below](#vendored-scoring-engine). |
| `config/` | `funnel.yaml` (thresholds) and `subproject.*.yaml` (campaigns). |

### The circularity trap, and `air_homolog`

The default path extracts the interface residues from the Boltz model itself,
writes them as restraints, and then has `caprieval` measure DockQ against *that
same model*. That is circular: you tell HADDOCK where the interface is and then
ask whether it found it. In practice the result is near-perfect (fnat ≈ 1.0,
i-RMSD < 0.5 Å, DockQ > 0.95) — a number that only shows HADDOCK works, not that
the interface is real.

`funnel/air_homolog.py` exists for the other question. It derives the restraints
from an **experimental homologous complex** from the PDB, projects them onto your
model's numbering, and reports `coverage` and `residuos_nao_mapeados` as part of
the result — the fraction of the template interface with no counterpart in your
model belongs in the Methods, not swept under the rug. A DockQ obtained that way
can **refute** the prediction, which the circular path never could.

It ships with three public reference complexes as a starting point (1BRS, 1JTG,
3HFM); add the homolog of *your* pair to the `TEMPLATES` catalogue.

---

## Installing

Requires Python ≥ 3.11.

```bash
git clone https://github.com/luizdione/Platform_ppi_docking
cd Platform_ppi_docking
pip install -e .
```

Optional extras:

```bash
pip install -e ".[viz]"   # Plotly figures and the embedded 3D viewer
```

Run the test suite — it needs no data, no GPU and no network. The `[dev]` extra
is what brings the test dependencies, so install it first:

```bash
pip install -e ".[dev]" && pytest
```

From a bare clone that is what you get: **174 passed, 32 skipped, 0 failed.**
Every skip states its reason — no `[viz]` extra, no HADDOCK3 binary, no
container image, no engines repository, no data root declared. A skip is never a
silent pass. Add `[viz]`, the docking engines repository and a data root and the
skipped ones start running too.

Start the panel (default <http://localhost:8777>; the API mounts under `/api`):

```bash
python -m web
```

On Windows, `iniciar_painel.ps1` (or the `PPI-Docking.bat` shortcut) does the
same with a launcher.

---

## Pointing the funnel at your data

Nothing is hardcoded to a disk. The funnel reads what these variables point at,
and the one that matters most has **no default on purpose** — a fixed path here
would be the path of whoever wrote the code, never yours.

### Required

| Variable | What it is |
|---|---|
| `PPI_DOCK_DATA_ROOT` | **The single root under which all of your data lives** (Boltz-2 predictions, derived PDBs, HADDOCK3 runs). It is the one directory mounted into the HADDOCK3 container, so a `.cfg` referring to anything outside it is refused with a clear error rather than failing mid-run. Unset ⇒ immediate, explanatory error. |

### Campaigns and work directories

| Variable | Default | What it is |
|---|---|---|
| `PPI_SUBPROJECTS` | — | Path to a curated `subprojetos.json` index of campaigns. Optional: without it, campaigns come from the versioned `config/subproject.*.yaml`. |
| `PPI_DOCK_WORKDIR` | — | Root where the bridge prepares a pair (`<root>/<campaign>/<pair_id>`). The bridge never picks a destination on its own. |
| `PPI_DOCK_JOBS_DIR` | `~/.ppi_docking_jobs` | Where job logs go. |
| `PPI_DOCK_CACHE_DIR` | — | Cache for live-scored manifests. |
| `PPI_DOCK_TEMPLATE_DIR` | `~/.cache/ppi_docking/templates` | Cache for homolog templates fetched from the RCSB. |

### External engines

| Variable | Default | What it is |
|---|---|---|
| `DOCKING_SUITE_HOME` | `../Platform_docking_suite` | Root of the repository providing `engines.haddock3`. By default, a clone sitting next to this one. |
| `PPI_DOCK_HADDOCK_IMAGE` | `ppi-haddock3:latest` | Container image for the HADDOCK3 run. |
| `PPI_ENGINE_CLI` | — | Python module name of an **optional external CLI** for the stages that are *not* implemented here: prediction, molecular dynamics, MM/GBSA and reporting. Unset ⇒ those four panel actions fail with a clear message. The funnel itself does not use it. |
| `PPI_PY_ANALYSIS` | this interpreter | Interpreter for that external CLI's analysis stages. |
| `PPI_PY_PREDICT` | — | Interpreter of an environment with PyTorch/CUDA, for prediction. Required only for that. |
| `PPI_ENGINE_APP_DIR` | — | Directory holding an external `dashboard_core.py` that serves a few optional panel pages (environment diagnostics, experiment recipes, MD discovery). Not part of this repository; without it those pages explain why instead of breaking. |

### Panel and bridge

| Variable | Default |
|---|---|
| `PPI_WEB_HOST` / `PPI_WEB_PORT` | `127.0.0.1` / `8777` |
| `PPI_DOCK_BRIDGE_HOST` / `PPI_DOCK_BRIDGE_PORT` | `0.0.0.0` / `8778` (standalone bridge) |
| `PPI_DOCK_ENV` / `PPI_DOCK_CONDA` | conda environment name and `conda` executable, when the bridge calls the external CLI through `conda run` |
| `PPI_DOCK_TIMEOUT` | `900` seconds for a synchronous subprocess |
| `PPI_DOCS_DIRS` | extra documentation folders for the Help tab |
| `PPI_3DMOL_JS` | path to a local `3Dmol-min.js`, instead of fetching it once from a public CDN |

### Declaring a campaign

A campaign is one YAML in `config/`. It holds **pointers only** — where the runs
are, where the score spreadsheet is, where HADDOCK3 output should go — plus two
things the code must not guess:

* **`chain_roles`** — which chain is the bait/receptor and which is the
  partner/ligand, *declared*, not inferred. In a real complex the chain order in
  the `.cif` need not follow the pair's name, and ligand/cofactor chains usually
  come *after* the proteins; blindly applying "last chain = partner" would elect
  a cofactor. With `fail_if_ambiguous: true`, an unresolvable case raises instead
  of guessing.
* **`scores`** — the column→field mapping of *your* spreadsheet, and which ipTM
  field to consume. Use `protein_iptm`, not the global `iptm`: the global one
  includes ligands and cofactors and inflates the number.

`config/subproject.demo01.yaml` is a fully commented example to copy.

---

## The public example

The example campaign is **barnase × barstar (PDB [1BRS](https://www.rcsb.org/structure/1BRS))**,
with interface point mutants as the partner panel. It was chosen because it is
public, it is one of the most studied protein–protein complexes in the
literature, it appears in the Docking Benchmark, and an experimental structure
exists to serve as an independent reference.

**It is an example, not a result.** No data accompanies this repository: the
paths in `config/subproject.demo01.yaml` use a `<RAIZ>` placeholder for you to
replace, and the numbers under `hits:` are illustrative. The same goes for the
synthetic table in `tests/test_gate.py`: those 14 rows are test cases built to
cover the gate's decision branches, with the expected verdict derived by hand
from the thresholds — not measurements.

---

## External dependencies

### HADDOCK3

The compute engine behind HADDOCK3 is CNS, which **has no Windows build** (Linux
x86_64/aarch64 and macOS only; the PyPI package ships an sdist and declares only
macOS/POSIX). So:

* `prepare()` is pure logic — it writes the `.cfg` and the `ambig.tbl` — and runs
  anywhere;
* `run()` needs the real engine. On Linux/macOS a local `haddock3` binary works.
  Otherwise, use the container.

**Pull the published image** (no build needed):

```bash
docker pull ghcr.io/luizdione/ppi-haddock3:0.1.0
export PPI_DOCK_HADDOCK_IMAGE=ghcr.io/luizdione/ppi-haddock3:0.1.0
```

If that `pull` fails with `denied`, the package is not public yet — build it
yourself with the recipe below, which produces the same thing.

Or build it yourself from the recipe in this repository:

```bash
docker build -f docker/Dockerfile.haddock3 -t ppi-haddock3:latest .
```

The published image is built and pushed by
[`.github/workflows/publicar-imagem.yml`](.github/workflows/publicar-imagem.yml),
which runs when `docker/Dockerfile.haddock3` changes, when a release is
published, or on demand. Before pushing, that workflow **exports the built image
and fails if it contains any file that is not part of the base image** — the
claim below is enforced, not just asserted.

The image is a plain `python:3.11-slim` with `haddock3` installed from PyPI,
`WORKDIR /work` and `haddock3` as its entrypoint. It carries **no file from this
repository and no data**: the `Dockerfile` has no `COPY` and no `ADD` at all —
your data is *mounted* at run time, never baked in. You can run it directly:

```bash
docker run --rm -v "$PPI_DOCK_DATA_ROOT:/data" \
    -w /data/<campaign>/haddock/<pair> \
    ghcr.io/luizdione/ppi-haddock3:0.1.0 run-<pair>.cfg
```

`-w` matters: the `.cfg` refers to `receptor.pdb`, `ligand.pdb` and `ambig.tbl`
by name, so the same file works on the host and inside the container without
path translation. `funnel.haddock_docker` does all of this for you, including
rewriting the absolute host paths that the engine writes into the `.cfg`.

The container mounts `PPI_DOCK_DATA_ROOT` at `/data` and the funnel rewrites the
absolute host paths inside the `.cfg` accordingly. Nothing is copied, and nothing
leaves your root. Without the binary **and** without the image, `run()` fails
loudly and keeps the artefacts — it never produces a plausible wrong result.

### The docking engines repository

`engines.haddock3` lives in its own repository and is **not** duplicated here:

```bash
git clone https://github.com/luizdione/Platform_docking_suite   # next to this one
# or, anywhere:
export DOCKING_SUITE_HOME=/path/to/Platform_docking_suite
```

That package is not installable (its own `pyproject.toml` packages other
modules) and `engines/haddock3.py` uses absolute imports, so it is the
**repository root** that goes on `sys.path` — which `_ensure_engines_importable()`
handles, raising a clear error naming the URL if it cannot find it.

### Upstream and downstream

Complex prediction (Boltz-2) and molecular dynamics are **outside** this
repository, by design. The funnel starts from predictions that already exist on
disk, in the Boltz-2 layout:

```
<results_root>/boltz_results_<label>/predictions/<label>/
    <label>_model_0.cif
    confidence_<label>_model_0.json
    pae_<label>_model_0.npz
```

and it ends by declaring a pair ready for MD. `tools/md_streaming_analysis.py` is
a standalone helper for afterwards: streaming RMSD(t) + per-residue RMSF +
representative frame, reading frame by frame so a large trajectory does not have
to fit in RAM.

---

## Vendored scoring engine

`scoring_engine/` is a verbatim, trimmed copy of the modules this funnel imports
from the author's own MIT-licensed package `ppi` (`ppi_platform`), by Luiz Dione
Barbosa De-Melo (IFRJ, ORCID 0000-0003-2445-9943). They are vendored so that
this repository runs standalone, and **only** the modules actually imported were
copied — each file carries its own provenance header.

| Module | What it provides |
|---|---|
| `_common` | `.npz`/`.cif` readers and the single inter-chain interface search (biopython `NeighborSearch`) |
| `ipsae` | ipSAE, robust to disordered regions and mobile domains |
| `score` | per-complex metrics, for 1:1 complexes and for oligomers |
| `rank` | HIT/MAYBE/weak classification, ranked table and shortlist |
| `config` | the screening thresholds (`SCORE`) — rewritten from scratch, keeping only that block |
| `inputs` | Boltz-2 / AlphaFold3 input generation |

`config` and `inputs` are stdlib-only and import eagerly; the rest needs numpy
and biopython and must be imported explicitly, so `import scoring_engine` stays
cheap.

---

## A note on language

This is deliberate, not an oversight: **the public documentation is in English —
this README, the `LICENSE` and `CITATION.cff` — while the panel's interface and
the campaign configuration files are in Portuguese.**

The split follows what each one is for. The README, the licence and the citation
are how the project presents itself to anyone, so they are in English. The web
panel and the `config/*.yaml` files (including the `texto:` fields that the
panel's Help tab renders) are the author's working tools, read by the people who
run the funnel, and they stay in the language those people use. Code comments
and commit messages are in Portuguese for the same reason.

The `LICENSE` is bilingual, with the Portuguese text prevailing: it is the
version the Author adopted, and it is the one that invokes Brazilian law.

Please don't "fix" this by translating one half — a half-translated interface is
worse than either language.

## Third-party components

`web/static/` carries 3Dmol.js and plotly.js, each under its own licence. They
are **not** versioned here (~530 KB and ~4.7 MB): the panel recreates them on
first start, from the `plotly` Python package and from a public CDN, after which
it works offline.

---

## Licence and citation

Use is governed by [`LICENSE`](LICENSE) — a custom licence, not an OSI one:

* **personal use and use in teaching** — free, no permission needed;
* **use in research** — allowed **only with the author's prior written
  authorization**, requested by e-mail to <luiz.melo@ifrj.edu.br>;
* **commercial use** — not allowed;
* **redistribution, modification and derivative works** — not allowed.

Running the program just to get to know or evaluate it, without generating
research data or results, counts as personal use and needs no authorization.

If authorized research use leads to a publication, cite the software as declared
in [`CITATION.cff`](CITATION.cff):

> De-Melo, L.D.B. *Platform_ppi_docking: a confidence funnel from AI-predicted
> protein–protein complexes to MD-ready refined pairs*, version 0.1.0.
> Instituto Federal de Educação, Ciência e Tecnologia do Rio de Janeiro (IFRJ).
> <https://github.com/luizdione/Platform_ppi_docking>

Please also cite the methods the funnel stands on: Boltz-2 for the prediction,
HADDOCK3 for the refinement, ipSAE (Dunbrack *et al.*, 2025) and DockQ
(Basu & Wallner, *PLoS ONE* 2016) for the metrics.

---

## Author

**Luiz Dione Barbosa De-Melo** (De-Melo, L.D.B.)
Instituto Federal de Educação, Ciência e Tecnologia do Rio de Janeiro (IFRJ),
Rio de Janeiro, Brasil
ORCID [0000-0003-2445-9943](https://orcid.org/0000-0003-2445-9943) ·
<luiz.melo@ifrj.edu.br>
