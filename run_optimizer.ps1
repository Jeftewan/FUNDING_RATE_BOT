<#
.SYNOPSIS
    Lanza la re-optimización del scoring en LOCAL (manual).

.DESCRIPTION
    Operación local: NO despliega ni corre en Railway. Solo lee la DB
    PostgreSQL de Railway (read-only) vía DATABASE_URL para descargar el
    histórico de funding y buscar mejores pesos del scoring.

    El script solo genera un candidato + reporte en reports/. No modifica
    analysis/scoring.py: la adopción es manual tras revisar el reporte.

    Carga DATABASE_URL desde un archivo .env local (KEY=VALUE por línea) si
    existe; si no, usa la variable de entorno actual.

.EXAMPLE
    .\run_optimizer.ps1
    Run completo (600 trials).

.EXAMPLE
    .\run_optimizer.ps1 --trials 20
    Prueba rápida. Cualquier flag se reenvía a scripts/scoring_optimizer.py
    (--trials, --days, --force-reload).
#>

$ErrorActionPreference = "Stop"
$RepoRoot = $PSScriptRoot

# ── Cargar .env si existe (solo si DATABASE_URL no está ya en el entorno) ──
$envFile = Join-Path $RepoRoot ".env"
if (-not $env:DATABASE_URL -and (Test-Path $envFile)) {
    Write-Host "Cargando variables de .env..."
    foreach ($line in Get-Content $envFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $kv = $trimmed -split "=", 2
        if ($kv.Count -eq 2) {
            $key = $kv[0].Trim()
            $val = $kv[1].Trim().Trim('"').Trim("'")
            Set-Item -Path "Env:$key" -Value $val
        }
    }
}

if (-not $env:DATABASE_URL) {
    Write-Error "DATABASE_URL no está definida. Ponla en .env o en el entorno antes de correr."
    exit 1
}

# ── Verificar dependencias de dev ──
python -c "import optuna, pandas, sqlalchemy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Faltan dependencias. Corre:  pip install -r requirements-dev.txt"
    exit 1
}

# ── Ejecutar el optimizador, reenviando todos los flags ──
Write-Host "Lanzando optimizador (local, read-only sobre la DB)..." -ForegroundColor Cyan
python (Join-Path $RepoRoot "scripts\scoring_optimizer.py") @args
$rc = $LASTEXITCODE

if ($rc -eq 0) {
    Write-Host "`nListo. Revisa los artefactos en:  $(Join-Path $RepoRoot 'reports')" -ForegroundColor Green
}
exit $rc
