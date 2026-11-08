# -*- coding: utf-8 -*-
"""funnel/haddock_docker.py -- executa o HADDOCK3 num container Linux.

POR QUE ESTE MODULO EXISTE
--------------------------
O motor de calculo do HADDOCK3 e o CNS, que **nao tem build para Windows**
(arquiteturas suportadas: Linux x86_64/aarch64 e macOS; o pacote `haddock3` no
PyPI publica so sdist e declara apenas macOS/POSIX nos classificadores). Como o
resto da plataforma pode rodar no Windows, o
`prepare` (logica pura: gera `.cfg` + `ambig.tbl`) acontece no HOST e so o `run`
atravessa para um container Linux. Imagem: `docker/Dockerfile.haddock3`.

O PROBLEMA QUE ESTE MODULO RESOLVE
----------------------------------
O `.cfg` gerado pelo motor (`engines/haddock3.py: build_cfg`) grava caminhos
ABSOLUTOS do host::

    molecules = [
        "<raiz-de-dados>/<campanha>/haddock/<par>/pdbs/receptor.pdb",
        ...
    ]
    [caprieval]
    reference_fname = "<raiz-de-dados>/<campanha>/boltz_results_.../..._model_0.cif"

Dentro do container esses caminhos NAO EXISTEM. Rodar assim faria o HADDOCK3
falhar ao abrir as moleculas -- ou, num cenario pior, seguir sem a referencia do
caprieval e produzir um `capri_clt.tsv` auto-referenciado (numeros plausiveis e
errados). A regra do projeto e nunca degradar em silencio.

Solucao: como TODO o dado de uma instalacao vive sob UMA unica raiz (a env
``PPI_DOCK_DATA_ROOT``), basta **uma montagem** dessa raiz em `/data` e a
traducao do prefixo dentro do `.cfg`. Nada e copiado, nada sai da raiz.

    host      <PPI_DOCK_DATA_ROOT>\\<campanha>\\haddock\\<par>\\pdbs\\receptor.pdb
    container /data/<campanha>/haddock/<par>/pdbs/receptor.pdb

O `.cfg` original NAO e alterado (fica para inspecao no host); geramos um
`<nome>.container.cfg` ao lado, que e o efetivamente executado.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

__all__ = [
    "DataRoot",
    "DockerRunResult",
    "data_root",
    "image_name",
    "to_container_path",
    "translate_cfg",
    "build_command",
    "run_haddock3",
    "check_image",
]

# Raiz unica do dado no host. NAO tem default: um caminho fixo aqui apontaria
# para o disco de quem escreveu o codigo. Declare a env PPI_DOCK_DATA_ROOT.
DATA_ROOT_ENV = "PPI_DOCK_DATA_ROOT"
# Ponto de montagem dentro do container (WORKDIR da imagem e /work).
_CONTAINER_DATA = "/data"
_DEFAULT_IMAGE = "ppi-haddock3:latest"


@dataclass
class DataRoot:
    """Par (raiz no host, raiz no container) usado nas traducoes."""

    host: Path
    container: str = _CONTAINER_DATA


@dataclass
class DockerRunResult:
    """Resultado da execucao. `status`: ok|error (espelha EngineResult)."""

    status: str = "ok"
    cmdline: "list[str]" = field(default_factory=list)
    returncode: "int | None" = None
    cfg_container: "str | None" = None
    log: str = ""
    error: "str | None" = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and not self.error


def data_root() -> DataRoot:
    """Raiz do dado no host, lida da env ``PPI_DOCK_DATA_ROOT``.

    OBRIGATORIA e sem default: e a unica raiz montada no container, e
    adivinhar um disco produziria um `.cfg` que aponta para fora da montagem
    (arquivo nao encontrado no meio da execucao). Ausente = erro imediato.
    """
    bruto = os.environ.get(DATA_ROOT_ENV, "").strip()
    if not bruto:
        raise RuntimeError(
            f"{DATA_ROOT_ENV} nao definida. Essa variavel aponta a raiz UNICA onde "
            "vivem os seus dados (predicoes do Boltz-2, PDBs derivados, runs do "
            "HADDOCK3); e ela que e montada em /data dentro do container. "
            "Exemplo: set " + DATA_ROOT_ENV + "=D:\\dados\\ppi (Windows) ou "
            "export " + DATA_ROOT_ENV + "=/srv/ppi (Linux/macOS)."
        )
    return DataRoot(host=Path(bruto))


def image_name() -> str:
    """Nome da imagem do HADDOCK3 (env ``PPI_DOCK_HADDOCK_IMAGE``)."""
    return os.environ.get("PPI_DOCK_HADDOCK_IMAGE", _DEFAULT_IMAGE)


def _normalizar(texto: str) -> str:
    """Caminho em forma comparavel: barras `/` e minusculas (Windows e case-insensitive)."""
    return str(texto).replace("\\", "/").rstrip("/").lower()


def to_container_path(caminho: "str | Path", raiz: "DataRoot | None" = None) -> str:
    """Traduz um caminho do host para o caminho equivalente no container.

    Levanta ``ValueError`` se o caminho estiver FORA da raiz montada -- de
    proposito: um `.cfg` que referencia algo fora da raiz de dados nao
    seria visivel no container, e falhar aqui e melhor do que descobrir isso
    como "arquivo nao encontrado" no meio da execucao.
    """
    raiz = raiz or data_root()
    alvo = _normalizar(caminho)
    base = _normalizar(raiz.host)
    if not (alvo == base or alvo.startswith(base + "/")):
        raise ValueError(
            f"caminho fora da raiz montada: {caminho!r}\n"
            f"raiz do host: {raiz.host} (montada em {raiz.container}).\n"
            "Todo dado deve viver sob essa raiz; ajuste "
            f"{DATA_ROOT_ENV} ou mova o arquivo para dentro dela."
        )
    resto = str(caminho).replace("\\", "/")[len(str(raiz.host).replace("\\", "/")):]
    return (raiz.container + "/" + resto.lstrip("/")).rstrip("/")


# Casa qualquer caminho absoluto do Windows entre aspas: "E:/..." ou "E:\...".
_CAMINHO_ABS = re.compile(r'"([A-Za-z]:[\\/][^"]*)"')


def translate_cfg(
    cfg_path: "str | Path",
    raiz: "DataRoot | None" = None,
    *,
    sufixo: str = ".container.cfg",
) -> Path:
    """Escreve, ao lado do `.cfg`, uma versao com os caminhos do container.

    Reescreve TODO caminho absoluto de Windows entre aspas (cobre `molecules` e
    `reference_fname` de uma vez, sem depender da ordem/nome das chaves). O
    arquivo original permanece intacto para inspecao no host.
    """
    raiz = raiz or data_root()
    origem = Path(cfg_path)
    if not origem.is_file():
        raise FileNotFoundError(f"cfg do HADDOCK3 nao encontrado: {origem}")

    texto = origem.read_text(encoding="utf-8")
    achados: "list[str]" = []

    def _troca(m: "re.Match[str]") -> str:
        achados.append(m.group(1))
        return '"' + to_container_path(m.group(1), raiz) + '"'

    convertido = _CAMINHO_ABS.sub(_troca, texto)

    if not achados:
        raise ValueError(
            f"{origem.name}: nenhum caminho absoluto encontrado para traduzir. "
            "Esperava ao menos as entradas de `molecules`. O .cfg parece "
            "incompleto ou ja traduzido -- nao vou rodar as cegas."
        )

    destino = origem.with_suffix("")
    destino = destino.with_name(destino.name + sufixo)
    destino.write_text(convertido, encoding="utf-8")
    return destino


def build_command(
    cfg_container: str,
    workdir_container: str,
    raiz: "DataRoot | None" = None,
    *,
    imagem: "str | None" = None,
    ncores: "int | None" = None,
    memory: "str | None" = None,
) -> "list[str]":
    """Monta o argv do `docker run` (nunca `shell=True`).

    Monta a raiz do dado como volume e roda com `-w` na pasta do par, de modo
    que `ambig_fname = "ambig.tbl"` (relativo, como o motor gera) resolva.

    ``ncores`` / ``memory`` (ex.: ``"1500m"``, ``"2g"``) viram ``--cpus`` e
    ``--memory``. O teto de memoria importa quando o Docker roda dentro de uma
    VM (WSL2 no Windows, por exemplo): o container divide aquele espaco com o
    que ja estiver rodando. Com `--memory`, um HADDOCK que cresca demais e
    morto SO ELE (OOM do container), em vez de empurrar a maquina para swap --
    a regra aqui e nunca estourar a RAM do host.
    """
    raiz = raiz or data_root()
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{raiz.host}:{raiz.container}",
        "-w", workdir_container,
    ]
    if ncores:
        # o HADDOCK3 e CPU-bound; limitar evita competir com Boltz/MD no host
        cmd += ["--cpus", str(ncores)]
    if memory:
        cmd += ["--memory", str(memory)]
    cmd += [imagem or image_name(), cfg_container]
    return cmd


#: Pedacos de stderr que so aparecem quando o DAEMON esta fora do ar (Docker
#: Desktop fechado), nao quando a imagem falta. Sem isso, um Docker parado era
#: relatado como "imagem ausente" e mandava o usuario reconstruir uma imagem
#: que ja existe -- conselho caro e errado.
_DAEMON_FORA = (
    "cannot connect to the docker daemon",
    "failed to connect to the docker api",
    "error during connect",
    "the docker client must be run with elevated privileges",
    "open //./pipe/docker",
)


def check_image(imagem: "str | None" = None) -> dict:
    """Confere se a imagem existe localmente. Nao constroi nada.

    Distingue os dois "nao disponivel" que exigem acoes opostas: **daemon
    parado** (abrir o Docker Desktop) e **imagem ausente** (construir). O campo
    ``motivo`` diz qual e, para a UI nao ter que adivinhar pelo texto.
    """
    nome = imagem or image_name()
    try:
        proc = subprocess.run(
            ["docker", "image", "inspect", nome],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return {"available": False, "image": nome, "motivo": "sem_docker",
                "error": "docker nao encontrado no PATH"}
    except Exception as exc:  # noqa: BLE001 -- diagnostico, nao pode explodir
        return {"available": False, "image": nome, "motivo": "erro",
                "error": repr(exc)}
    if proc.returncode != 0:
        saida = f"{proc.stderr or ''} {proc.stdout or ''}".lower()
        if any(marca in saida for marca in _DAEMON_FORA):
            return {
                "available": False, "image": nome, "motivo": "daemon_parado",
                "error": ("o servico do Docker nao esta no ar (Docker Desktop "
                          "fechado?). Abra o Docker Desktop e tente de novo -- a "
                          "imagem pode muito bem ja existir."),
            }
        return {
            "available": False, "image": nome, "motivo": "imagem_ausente",
            "error": (f"imagem '{nome}' nao encontrada localmente. Construa com: "
                      "docker build -f docker/Dockerfile.haddock3 "
                      "-t ppi-haddock3:latest ."),
        }
    return {"available": True, "image": nome, "motivo": "ok"}


def run_haddock3(
    cfg_path: "str | Path",
    raiz: "DataRoot | None" = None,
    *,
    imagem: "str | None" = None,
    ncores: "int | None" = None,
    memory: "str | None" = None,
    timeout_s: int = 86400,
    dry_run: bool = False,
) -> DockerRunResult:
    """Roda `haddock3 <cfg>` no container, sobre a pasta do par.

    Nunca mascara falha: imagem ausente, caminho fora da raiz ou exit code != 0
    viram ``DockerRunResult(status="error")`` com mensagem util.
    """
    raiz = raiz or data_root()
    origem = Path(cfg_path)

    try:
        cfg_traduzido = translate_cfg(origem, raiz)
        cfg_ct = to_container_path(cfg_traduzido, raiz)
        work_ct = to_container_path(origem.parent, raiz)
    except (FileNotFoundError, ValueError) as exc:
        return DockerRunResult(status="error", error=str(exc))

    cmd = build_command(cfg_ct, work_ct, raiz, imagem=imagem, ncores=ncores,
                        memory=memory)

    if dry_run:
        return DockerRunResult(status="ok", cmdline=cmd, cfg_container=cfg_ct,
                               log="dry_run: comando montado, nada executado")

    disponivel = check_image(imagem)
    if not disponivel.get("available"):
        return DockerRunResult(status="error", cmdline=cmd, cfg_container=cfg_ct,
                               error=disponivel.get("error"))

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return DockerRunResult(status="error", cmdline=cmd, cfg_container=cfg_ct,
                               error=f"haddock3 excedeu {timeout_s}s no container")
    except Exception as exc:  # noqa: BLE001
        return DockerRunResult(status="error", cmdline=cmd, cfg_container=cfg_ct,
                               error=f"docker run falhou: {exc!r}")

    if proc.returncode != 0:
        return DockerRunResult(
            status="error", cmdline=cmd, cfg_container=cfg_ct,
            returncode=proc.returncode,
            error=f"haddock3 saiu com codigo {proc.returncode}",
            log=(proc.stderr or proc.stdout or "")[-2000:],
        )
    return DockerRunResult(status="ok", cmdline=cmd, cfg_container=cfg_ct,
                           returncode=0, log=(proc.stdout or "")[-2000:])
