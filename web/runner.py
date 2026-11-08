# -*- coding: utf-8 -*-
"""Executor das acoes do painel. Roda como PROCESSO, um por job.

    python -m web.runner funil <key> [--ate gate|prepare|run]
    python -m web.runner haddock <key>

Cada acao imprime o progresso em stdout (sem buffer), porque o log ao vivo do
painel e exatamente esse stdout redirecionado para o arquivo do job. O motor
(`web/jobs.py`) so cuida de fila, travas e ciclo de vida; o que fazer mora aqui.

Sai com 0 no sucesso e != 0 na falha -- o estado do job vem do codigo de saida,
entao NUNCA engolir excecao e sair 0.

ETAPAS DE FORA DO FUNIL (opcionais)
-----------------------------------
`boltz` (predicao), `md` (dinamica molecular), `energia` (MM/GBSA) e `relatorio`
ficam ANTES e DEPOIS do funil na cascata cientifica, e nao sao implementadas
aqui: elas delegam a um CLI externo, declarado na env ``PPI_ENGINE_CLI`` como
nome de modulo python (ex.: ``meu_motor.cli``). Sem essa env, essas quatro acoes
falham com mensagem clara em vez de fingir que rodaram -- o funil em si (`funil`
e `haddock`) nao depende de nada disso.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

#: Modulo python do CLI externo que roda predicao/MD/energia/relatorio.
ENGINE_CLI_ENV = "PPI_ENGINE_CLI"

#: Interpretadores por ambiente. A analise (MD, energia, relatorio) roda no
#: python da env ``PPI_PY_ANALYSIS`` (default: o deste processo); a PREDICAO
#: exige um ambiente com PyTorch/CUDA, declarado em ``PPI_PY_PREDICT``. Os dois
#: sao separados de proposito: nenhum default aponta para um disco especifico.
PY_ANALYSIS_ENV = "PPI_PY_ANALYSIS"
PY_PREDICT_ENV = "PPI_PY_PREDICT"


def _p(msg: str) -> None:
    print(msg, flush=True)


def _python(env_var: str, *, obrigatorio: bool = False) -> str:
    """Interpretador declarado em `env_var`; cai no deste processo se opcional."""
    bruto = os.environ.get(env_var, "").strip()
    if bruto:
        if not Path(bruto).is_file():
            raise SystemExit(
                f"{env_var} aponta para um executavel inexistente: {bruto}"
            )
        return bruto
    if obrigatorio:
        raise SystemExit(
            f"{env_var} nao definida. Essa etapa precisa de um interpretador "
            "proprio (ambiente com PyTorch/CUDA para a predicao). "
            f"Ex.: set {env_var}=C:\\caminho\\para\\python.exe"
        )
    return sys.executable


def _engine_cli() -> str:
    """Nome do modulo do CLI externo (env ``PPI_ENGINE_CLI``). Ausente = erro."""
    bruto = os.environ.get(ENGINE_CLI_ENV, "").strip()
    if not bruto:
        raise SystemExit(
            f"{ENGINE_CLI_ENV} nao definida. Predicao, dinamica molecular, "
            "MM/GBSA e relatorio nao sao implementados neste repositorio: eles "
            "sao delegados a um CLI externo, informado como nome de modulo "
            f"python nessa variavel (ex.: set {ENGINE_CLI_ENV}=meu_motor.cli). "
            "O funil de confianca (acoes 'funil' e 'haddock') nao depende dela."
        )
    return bruto


def _rodar(cmd: "list[str]", rotulo: str) -> int:
    """Roda um comando repassando a saida ao log do job (stdout ja e o log)."""
    import subprocess

    _p(f"[{rotulo}] $ {' '.join(str(c) for c in cmd)}")
    proc = subprocess.run([str(c) for c in cmd], cwd=str(RAIZ), text=True)
    _p(f"[{rotulo}] rc={proc.returncode}")
    return proc.returncode


def acao_funil(args: argparse.Namespace) -> int:
    """Roda o funil de confianca de um subprojeto (score -> gate -> prepare)."""
    from funnel import pipeline

    _p(f"[funil] subprojeto={args.key} ate={args.ate}")
    resultado = pipeline.run_funnel(args.key, ate=args.ate)

    aprovados = resultado.get("aprovados") or []
    reprovados = resultado.get("reprovados") or []
    _p(f"[funil] aprovados={len(aprovados)} reprovados={len(reprovados)}")
    for item in aprovados:
        rotulo = item.get("pair_id") if isinstance(item, dict) else getattr(item, "pair_id", item)
        _p(f"[funil]   APROVADO {rotulo}")
    _p("[funil] concluido")
    return 0


def acao_haddock(args: argparse.Namespace) -> int:
    """Docking fisico (HADDOCK3) dos aprovados do funil, via container.

    Reusa o proprio pipeline do funil com `ate=run`: assim o HADDOCK roda
    exatamente sobre quem passou no GATE, sem lista paralela para desencontrar.
    """
    from funnel import pipeline

    _p(f"[haddock] subprojeto={args.key} (docking fisico dos aprovados)")
    resultado = pipeline.run_funnel(args.key, ate="run")
    aprovados = resultado.get("aprovados") or []
    _p(f"[haddock] pares processados: {len(aprovados)}")
    _p("[haddock] concluido")
    return 0


def acao_relatorio(args: argparse.Namespace) -> int:
    """Relatorio das predicoes escolhidas. Delega ao CLI externo."""
    cmd = [_python(PY_ANALYSIS_ENV), "-m", _engine_cli(), "report"] + list(args.runs)
    cmd += ["-o", args.saida]
    if args.projeto:
        cmd += ["--project", args.projeto]
    if args.figuras:
        cmd.append("--figures")
    if args.html:
        cmd.append("--html")
    if args.pdf:
        cmd.append("--pdf")
    return _rodar(cmd, "relatorio")


def acao_md(args: argparse.Namespace) -> int:
    """Dinamica molecular de UM par. Delega ao CLI externo.

    Resumivel: nunca use --fresh a toa.
    """
    cmd = [
        _python(PY_ANALYSIS_ENV), "-m", _engine_cli(), "md-run", args.run, args.md_dir,
        "--tier", args.tier, "--ff", args.ff,
    ]
    if args.ns:
        cmd += ["--ns", str(args.ns)]
    if args.replicas:
        cmd += ["--replicas", str(args.replicas)]
    if args.platform:
        cmd += ["--platform", args.platform]
    if args.fresh:
        _p("[md] ATENCAO: --fresh descarta o progresso anterior desta MD")
        cmd.append("--fresh")
    else:
        _p("[md] modo retomada (o padrao): se ja havia progresso, continua dali")
    return _rodar(cmd, "md")


def acao_energia(args: argparse.Namespace) -> int:
    """Energia livre MM/GBSA -- sobre a trajetoria da MD ou direto sobre a pose.

    Delega ao CLI externo.
    """
    py, cli = _python(PY_ANALYSIS_ENV), _engine_cli()
    if args.pose:
        cmd = [
            py, "-m", cli, "energy-pose", args.alvo,
            args.saida, "--ff", args.ff, "--gb", args.gb,
        ]
    else:
        cmd = [
            py, "-m", cli, "energy", args.alvo,
            "--frames", str(args.frames), "--gb", args.gb,
        ]
    if args.platform:
        cmd += ["--platform", args.platform]
    return _rodar(cmd, "energia")


def acao_boltz(args: argparse.Namespace) -> int:
    """Predicao de complexo (Boltz-2) em lote. Delega ao CLI externo, no
    interpretador do ambiente com GPU (``PPI_PY_PREDICT``)."""
    cmd = [
        _python(PY_PREDICT_ENV, obrigatorio=True), "-m", _engine_cli(),
        "predict", args.inputs, args.results,
    ]
    if args.diffusion_samples:
        cmd += ["--diffusion-samples", str(args.diffusion_samples)]
    return _rodar(cmd, "boltz")


_ACOES = {
    "boltz": acao_boltz,
    "funil": acao_funil,
    "haddock": acao_haddock,
    "md": acao_md,
    "energia": acao_energia,
    "relatorio": acao_relatorio,
}


def main(argv: "list[str]") -> int:
    p = argparse.ArgumentParser(prog="web.runner", description="acoes do painel")
    sub = p.add_subparsers(dest="acao", required=True)

    f = sub.add_parser("funil", help="funil de confianca de um subprojeto")
    f.add_argument("key", help="chave do subprojeto (ex.: DEMO01)")
    f.add_argument(
        "--ate",
        default="prepare",
        choices=["gate", "prepare", "run"],
        help="ate onde ir (default: prepare)",
    )

    b = sub.add_parser("boltz", help="predicao de complexo (Boltz-2, CLI externo)")
    b.add_argument("inputs", help="pasta com os YAML de entrada")
    b.add_argument("results", help="pasta de saida das predicoes")
    b.add_argument("--diffusion-samples", type=int, default=None)

    h = sub.add_parser("haddock", help="docking fisico dos aprovados do funil")
    h.add_argument("key", help="chave do subprojeto (ex.: DEMO01)")

    m = sub.add_parser("md", help="dinamica molecular de um par (CLI externo)")
    m.add_argument("run", help="pasta boltz_results_* ou .cif/.pdb da pose")
    m.add_argument("md_dir", help="pasta de saida da MD")
    m.add_argument("--tier", default="fast", choices=["fast", "deep"])
    m.add_argument("--ff", default="charmm36m", choices=["charmm36m", "ff19SB"])
    m.add_argument("--ns", type=float, default=None)
    m.add_argument("--replicas", type=int, default=None)
    m.add_argument("--platform", default=None)
    m.add_argument(
        "--fresh",
        action="store_true",
        help="recomeca do zero (descarta o progresso; o padrao e retomar)",
    )

    e = sub.add_parser("energia", help="energia livre MM/GBSA (trajetoria ou pose)")
    e.add_argument("alvo", help="pasta da MD (trajetoria) ou run/.cif (pose)")
    e.add_argument("--pose", action="store_true", help="rescoring da pose, sem MD")
    e.add_argument("--saida", default="", help="pasta de trabalho (modo pose)")
    e.add_argument("--frames", type=int, default=50)
    e.add_argument("--ff", default="ff19SB", choices=["ff19SB", "ff14SB"])
    e.add_argument("--gb", default="obc2", choices=["obc2", "gbn2"])
    e.add_argument("--platform", default=None)

    rel = sub.add_parser("relatorio", help="relatorio das predicoes (CLI externo)")
    rel.add_argument("--saida", required=True, help="arquivo .md de saida")
    rel.add_argument("--projeto", default="")
    rel.add_argument("--figuras", action="store_true")
    rel.add_argument("--html", action="store_true")
    rel.add_argument("--pdf", action="store_true")
    rel.add_argument("runs", nargs="+", help="pastas boltz_results_* a incluir")

    args = p.parse_args(argv)
    try:
        return _ACOES[args.acao](args)
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - o traceback vai para o log do job
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
