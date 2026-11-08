# ---------------------------------------------------------------------------
# Painel PPI - abre o painel local (um clique).
#
# Sobe a app FastAPI (`python -m web`) em http://localhost:8777 e abre o
# navegador. UMA porta so: a interface e a API de execucao moram na mesma app
# (a ponte antiga da 8778 foi absorvida em /api).
#
# Se o painel ja estiver no ar, so abre o navegador. Se a 8777 estiver ocupada
# por OUTRA coisa (o Streamlit antigo, por exemplo), avisa em vez de brigar pela
# porta. Script e codigo sao COLOCADOS neste repo -> $PSScriptRoot e a raiz.
# ---------------------------------------------------------------------------
$ErrorActionPreference = "Stop"

$raiz  = $PSScriptRoot
$porta = 8777
$url   = "http://localhost:$porta"

function Test-PortaEmUso([int]$p) {
    return ($null -ne (Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue))
}

function Test-PainelNoAr() {
    try {
        $r = Invoke-RestMethod -Uri "$url/health" -TimeoutSec 5
        return [bool]$r.ok
    } catch { return $false }
}

Write-Host "=== Painel PPI ===" -ForegroundColor Cyan

# --- 0. ja esta no ar? -----------------------------------------------------
if (Test-PainelNoAr) {
    Write-Host "Painel ja no ar. Abrindo $url" -ForegroundColor Green
    Start-Process $url
    Start-Sleep -Seconds 1
    exit 0
}

# --- 1. a porta esta ocupada por outra coisa? ------------------------------
if (Test-PortaEmUso $porta) {
    Write-Host "AVISO: a porta $porta esta ocupada, mas nao e o painel." -ForegroundColor Yellow
    Write-Host "       Provavelmente e o Streamlit antigo (atalho 'PPI-Docking')."
    Write-Host "       Feche-o e rode este atalho de novo."
    Read-Host "Enter para fechar"
    exit 1
}

# --- 2. interpretador de Python -------------------------------------------
$py = $env:PPI_DOCKING_PYTHON
# Sem a env, usa o python do PATH. Para um ambiente conda especifico, aponte
# PPI_DOCKING_PYTHON para o python.exe dele.
if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $py -or -not (Test-Path $py)) {
    Write-Host "ERRO: nao encontrei um interpretador de Python." -ForegroundColor Red
    if ($py) { Write-Host "      tentei: $py" }
    Write-Host "Defina PPI_DOCKING_PYTHON com o caminho do python.exe e rode de novo."
    Read-Host "Enter para fechar"
    exit 1
}

# --- 3. sobe o painel (oculto) ---------------------------------------------
Write-Host "subindo o painel em $url ..." -ForegroundColor Green
$logDir = Join-Path $env:LOCALAPPDATA "ppi_painel"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "painel.log"
Start-Process -FilePath $py -ArgumentList @("-m", "web") `
    -WorkingDirectory $raiz -WindowStyle Hidden `
    -RedirectStandardOutput $log -RedirectStandardError "$log.err"

# --- 4. espera responder e abre o navegador --------------------------------
Write-Host "aguardando o painel responder..." -ForegroundColor DarkGray
$ok = $false
for ($i = 0; $i -lt 45; $i++) {
    if (Test-PainelNoAr) { $ok = $true; break }
    Start-Sleep -Seconds 1
}

if ($ok) {
    Start-Process $url
    Write-Host ""
    Write-Host "=== no ar ===" -ForegroundColor Cyan
    Write-Host "  Painel : $url"
    Write-Host "  Jobs   : $url/jobs"
    Write-Host "  Log    : $log"
    Write-Host ""
    Write-Host "Na primeira subida o painel pontua os subprojetos em segundo plano"
    Write-Host "(alguns minutos); depois disso abre na hora, mesmo apos reiniciar."
    Write-Host "Para parar: feche o processo python do painel (ou reinicie a maquina)."
} else {
    Write-Host "AVISO: o painel nao respondeu em 45s. Veja o log:" -ForegroundColor Yellow
    Write-Host "       $log"
    Write-Host "       $log.err"
    Read-Host "Enter para fechar"
}
Start-Sleep -Seconds 2
