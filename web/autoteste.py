# -*- coding: utf-8 -*-
"""Autoteste da plataforma: prova que as pecas realmente funcionam.

O diagnostico "passivo" (versao de pacote, ferramenta no PATH) responde se algo
esta INSTALADO. Isto aqui responde a pergunta que importa antes de gastar horas
de GPU: **as pecas funcionam e conversam entre si?**

Cinco frentes, cada uma com verificacoes que EXECUTAM de verdade:

* ambiente   - o motor de pontuacao importa e o nucleo de dados nao
               depende de framework de UI;
* scripts    - toda acao do painel responde a `--help` (argv valido, imports
               resolvem);
* formatos   - ida e volta real em cada formato que a plataforma le/escreve:
               YAML de entrada, JSON de confianca, TSV de scores, CIF, manifesto;
* transicoes - as maquinas de estado: o estagio do par no funil e o ciclo de
               vida do job (fila -> rodando -> concluido; falha -> erro);
* motores    - GPU e HADDOCK3.

Cada verificacao devolve `ok`, um `detalhe` legivel e o `erro` quando falha --
nunca um "OK" generico que esconde o que nao foi testado.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List

RAIZ = Path(__file__).resolve().parent.parent


def _v(nome: str, fn: "Callable[[], str]") -> "Dict[str, Any]":
    """Roda uma verificacao e captura o desfecho (excecao nunca vaza)."""
    inicio = time.monotonic()
    try:
        detalhe = fn() or "ok"
        return {
            "nome": nome, "ok": True, "detalhe": detalhe, "erro": "",
            "ms": round((time.monotonic() - inicio) * 1000),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "nome": nome, "ok": False, "detalhe": "",
            "erro": f"{type(exc).__name__}: {exc}",
            "ms": round((time.monotonic() - inicio) * 1000),
        }




def _com_jobs_dir_temporario(fn: "Callable[[str], Any]") -> Any:
    """Roda `fn` com o diretorio de jobs isolado (nao suja o historico real)."""
    with tempfile.TemporaryDirectory() as pasta:
        antes = os.environ.get("PPI_DOCK_JOBS_DIR")
        os.environ["PPI_DOCK_JOBS_DIR"] = pasta
        try:
            return fn(pasta)
        finally:
            if antes is None:
                os.environ.pop("PPI_DOCK_JOBS_DIR", None)
            else:
                os.environ["PPI_DOCK_JOBS_DIR"] = antes


# ---------------------------------------------------------------------------
# 1. ambiente
# ---------------------------------------------------------------------------
def _bloco_ambiente() -> "List[Dict[str, Any]]":
    def python_do_painel() -> str:
        return f"{sys.version.split()[0]} ({Path(sys.executable).parents[1].name})"

    def motor_de_pontuacao() -> str:
        from scoring_engine import score  # exige numpy + biopython

        if not hasattr(score, "score_case"):
            raise AssertionError("scoring_engine.score sem score_case")
        return "scoring_engine.score importado (numpy + biopython ok)"

    def interprete_de_predicao() -> str:
        from web.runner import PY_PREDICT_ENV

        bruto = os.environ.get(PY_PREDICT_ENV, "").strip()
        if not bruto:
            raise FileNotFoundError(
                f"{PY_PREDICT_ENV} nao definida (opcional: so a predicao "
                "Boltz-2 precisa dela, e ela roda fora deste repositorio)"
            )
        if not Path(bruto).is_file():
            raise FileNotFoundError(f"nao achei {bruto}")
        return str(bruto)

    def nucleo_sem_ui() -> str:
        fonte = (RAIZ / "core" / "data.py").read_text(encoding="utf-8")
        if "import streamlit" in fonte:
            raise AssertionError("core/data.py voltou a depender de Streamlit")
        return "core/data.py nao importa framework de UI"

    return [
        _v("Python do painel", python_do_painel),
        _v("motor de pontuacao importavel", motor_de_pontuacao),
        _v("interprete de predicao (opcional)", interprete_de_predicao),
        _v("nucleo de dados sem UI", nucleo_sem_ui),
    ]


# ---------------------------------------------------------------------------
# 2. scripts (argv valido, imports resolvem)
# ---------------------------------------------------------------------------
_ACOES = ("boltz", "funil", "haddock", "md", "energia", "relatorio")


def _bloco_scripts() -> "List[Dict[str, Any]]":
    def acao(nome: str):
        def _f() -> str:
            proc = subprocess.run(
                [sys.executable, "-m", "web.runner", nome, "--help"],
                cwd=str(RAIZ), capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or "")[-200:])
            return "argv aceito"
        return _f

    itens = [_v(f"acao {nome}", acao(nome)) for nome in _ACOES]

    def cli_externo() -> str:
        """O CLI externo das etapas de fora do funil, se declarado."""
        from web.runner import ENGINE_CLI_ENV, PY_ANALYSIS_ENV

        modulo = os.environ.get(ENGINE_CLI_ENV, "").strip()
        if not modulo:
            raise RuntimeError(
                f"{ENGINE_CLI_ENV} nao definida (opcional: predicao, MD, "
                "MM/GBSA e relatorio rodam fora deste repositorio)"
            )
        py = os.environ.get(PY_ANALYSIS_ENV, "").strip() or sys.executable
        proc = subprocess.run(
            [py, "-m", modulo, "--help"],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "")[-200:])
        return f"{modulo} responde a --help"

    itens.append(_v("CLI externo (opcional)", cli_externo))
    return itens


# ---------------------------------------------------------------------------
# 3. formatos (ida e volta de verdade)
# ---------------------------------------------------------------------------
def _bloco_formatos() -> "List[Dict[str, Any]]":
    def yaml_entrada() -> str:
        import yaml

        from scoring_engine import inputs

        # Formato REAL da entidade do estagio 2: dict plano com 'type', nao o
        # aninhamento do dialeto AF3. Leva um ligante CCD junto porque cofator
        # (GTP/Mg) e rotina aqui -- e o caminho que mais quebra em silencio.
        texto = inputs.boltz_complex(
            [
                {"type": "protein", "id": "A", "sequence": "MKTAYIAKQRQISFVKSHFS"},
                {"type": "protein", "id": "B", "sequence": "MGSSHHHHHHSSGLVPRGSH"},
                {"type": "ligand", "id": "C", "ccd": "GTP"},
            ]
        )
        carregado = yaml.safe_load(texto)
        cadeias = [list(e)[0] for e in carregado["sequences"]]
        if cadeias != ["protein", "protein", "ligand"]:
            raise AssertionError(f"YAML inesperado: {cadeias}")
        if carregado["sequences"][2]["ligand"].get("ccd") != "GTP":
            raise AssertionError("ligante CCD nao sobreviveu ao YAML")
        return f"{len(texto)} bytes, 2 proteinas + ligante CCD, relido por yaml"

    def json_confianca() -> str:
        exemplo = {
            "confidence_score": 0.87, "ptm": 0.83, "iptm": 0.81,
            "protein_iptm": 0.80, "complex_plddt": 0.89,
        }
        with tempfile.TemporaryDirectory() as d:
            alvo = Path(d) / "confidence_x_model_0.json"
            alvo.write_text(json.dumps(exemplo), encoding="utf-8")
            lido = json.loads(alvo.read_text(encoding="utf-8"))
        if lido.get("protein_iptm") != 0.80:
            raise AssertionError("ipTM_pp nao sobreviveu ao ciclo")
        return "chaves de confianca preservadas (incl. protein_iptm)"

    def tsv_scores() -> str:
        from core.data import Subproject, load_scores_tsv

        with tempfile.TemporaryDirectory() as d:
            alvo = Path(d) / "s.tsv"
            alvo.write_text(
                "label\tipSAE\tipTMpp\nPAR_A\t0.616\t0.806\n", encoding="utf-8"
            )
            # A planilha e SEMPRE lida atraves do Subproject: o mapeamento
            # coluna->campo mora no YAML da campanha, nunca no codigo.
            sub = Subproject(
                key="AUTOTESTE",
                name="autoteste",
                campaign="autoteste",
                scores_tsv=str(alvo),
                scores_columns={"label": "label", "ipsae": "ipSAE", "iptm": "ipTMpp"},
            )
            linhas = load_scores_tsv(sub)
        if "PAR_A" not in linhas:
            raise AssertionError(f"TSV nao mapeou a chave: {list(linhas)}")
        if abs((linhas["PAR_A"].get("ipsae") or 0.0) - 0.616) > 1e-9:
            raise AssertionError(f"ipSAE veio errado: {linhas['PAR_A']}")
        return f"1 linha lida, campos {sorted(linhas['PAR_A'])} (numericos convertidos)"

    def estrutura_cif() -> str:
        from Bio.PDB.Atom import Atom
        from Bio.PDB.Chain import Chain
        from Bio.PDB.MMCIFParser import MMCIFParser
        from Bio.PDB.mmcifio import MMCIFIO
        from Bio.PDB.Model import Model
        from Bio.PDB.Residue import Residue
        from Bio.PDB.Structure import Structure

        est = Structure("s")
        mod = Model(0)
        est.add(mod)
        for letra in ("A", "B"):
            cad = Chain(letra)
            mod.add(cad)
            for i in (1, 2):
                res = Residue((" ", i, " "), "ALA", "")
                res.add(
                    Atom("CA", (float(i), 0.0, 0.0), 20.0, 1.0, " ", "CA", i, "C")
                )
                cad.add(res)
        with tempfile.TemporaryDirectory() as d:
            alvo = Path(d) / "m.cif"
            io = MMCIFIO()
            io.set_structure(est)
            io.save(str(alvo))
            lido = MMCIFParser(QUIET=True).get_structure("x", str(alvo))
            cadeias = [c.id for c in lido[0]]
            atomos = sum(1 for _ in lido.get_atoms())
        if cadeias != ["A", "B"] or atomos != 4:
            raise AssertionError(f"CIF voltou {cadeias} / {atomos} atomos")
        return "CIF escrito e relido: 2 cadeias, 4 atomos"

    def manifesto_json() -> str:
        from funnel.contract import PairManifest

        m = PairManifest(campaign="C", pair_id="P", bait="A", partner="B")
        volta = PairManifest.from_dict(json.loads(json.dumps(m.to_dict())))
        if volta.pair_id != "P":
            raise AssertionError("manifesto nao sobreviveu ao ciclo JSON")
        return "PairManifest -> JSON -> PairManifest"

    return [
        _v("YAML de entrada (Boltz-2)", yaml_entrada),
        _v("JSON de confianca", json_confianca),
        _v("TSV de scores", tsv_scores),
        _v("estrutura CIF (escrita e leitura)", estrutura_cif),
        _v("manifesto do funil (JSON)", manifesto_json),
    ]


# ---------------------------------------------------------------------------
# 4. transicoes (maquinas de estado)
# ---------------------------------------------------------------------------
def _bloco_transicoes() -> "List[Dict[str, Any]]":
    def estagios_do_funil() -> str:
        from funnel.contract import PairManifest

        m = PairManifest(campaign="C", pair_id="P", bait="A", partner="B")
        vistos = [m.stage]
        for estagio in ("scored", "gated", "prepared", "docked"):
            m.stage = estagio
            vistos.append(m.stage)
        return " -> ".join(vistos)

    def _roda_job(cmd: "List[str]") -> "tuple":
        from web import jobs as motor

        def _dentro(_pasta: str) -> "tuple":
            job = motor.enfileirar("autoteste", cmd, classe="leve")
            limite = time.time() + 40
            while time.time() < limite and job.estado in motor.VIVOS:
                motor._escalonar()
                time.sleep(0.2)
            estado, rc = job.estado, job.rc
            motor._JOBS.pop(job.id, None)
            return estado, rc

        return _com_jobs_dir_temporario(_dentro)

    def ciclo_do_job() -> str:
        from web import jobs as motor

        estado, rc = _roda_job([sys.executable, "-c", "print('ok')"])
        if estado != motor.CONCLUIDO or rc != 0:
            raise AssertionError(f"job terminou em {estado} (rc={rc})")
        return "na_fila -> rodando -> concluido (rc=0)"

    def falha_vira_erro() -> str:
        from web import jobs as motor

        estado, rc = _roda_job([sys.executable, "-c", "import sys; sys.exit(7)"])
        if estado != motor.ERRO or rc != 7:
            raise AssertionError(f"falha nao virou erro: {estado} (rc={rc})")
        return "saida != 0 vira erro (nunca concluido)"

    def trava_de_recurso() -> str:
        from web import jobs as motor

        if motor.LIMITES.get("gpu") != 1 or motor.LIMITES.get("cpu_heavy") != 1:
            raise AssertionError(f"limites afrouxaram: {motor.LIMITES}")
        return f"limites simultaneos: {motor.LIMITES}"

    return [
        _v("estagios do par no funil", estagios_do_funil),
        _v("ciclo de vida do job", ciclo_do_job),
        _v("falha do job vira erro", falha_vira_erro),
        _v("travas de recurso (GPU/CPU)", trava_de_recurso),
    ]


# ---------------------------------------------------------------------------
# 5. motores externos
# ---------------------------------------------------------------------------
def _bloco_motores() -> "List[Dict[str, Any]]":
    def gpu() -> str:
        from web.jobs import gpu_ocupada

        estado = gpu_ocupada()
        if not estado.get("sabe"):
            raise RuntimeError(estado.get("motivo", "sem leitura da GPU"))
        medida = (
            f"{estado.get('memoria_mib')} de {estado.get('total_mib')} MiB, "
            f"{estado.get('uso_pct')}% de uso"
        )
        if estado.get("ocupada"):
            return f"ocupada ({medida}) - job de GPU vai esperar"
        return f"livre ({medida})"

    def haddock() -> str:
        from core.data import haddock3_toolchain_status

        estado = haddock3_toolchain_status()
        if not estado.get("available"):
            raise RuntimeError(estado.get("detail") or "toolchain indisponivel")
        return f"motor: {estado.get('motor')}"

    return [
        _v("GPU (nvidia-smi)", gpu),
        _v("HADDOCK3 (binario ou container)", haddock),
    ]


# ---------------------------------------------------------------------------
BLOCOS = [
    ("Ambiente", _bloco_ambiente),
    ("Scripts do painel", _bloco_scripts),
    ("Formatos de arquivo", _bloco_formatos),
    ("Transicoes de estado", _bloco_transicoes),
    ("Motores externos", _bloco_motores),
]


def rodar() -> "Dict[str, Any]":
    """Roda o autoteste inteiro e devolve o resultado por bloco."""
    inicio = time.monotonic()
    blocos = []
    for titulo, fn in BLOCOS:
        itens = fn()
        blocos.append(
            {
                "titulo": titulo,
                "itens": itens,
                "ok": sum(1 for i in itens if i["ok"]),
                "total": len(itens),
            }
        )
    total = sum(b["total"] for b in blocos)
    passou = sum(b["ok"] for b in blocos)
    return {
        "blocos": blocos,
        "total": total,
        "passou": passou,
        "falhou": total - passou,
        "segundos": round(time.monotonic() - inicio, 1),
    }
