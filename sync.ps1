# sync.ps1 — push the project to the Pi using built-in scp/ssh (no rsync needed).
# Pushes code only (skips data/, .venv/, __pycache__ — those live on the Pi),
# then fixes permissions so scp's missing-write-bit issue can't recur.
#
#   Usage:   .\sync.ps1                       # uses defaults below
#            .\sync.ps1 -Pi user@host         # override target
#
param(
    [string]$Pi   = "lucasvitti@pi-cam.local",
    [string]$Dest = "~/counter_vision"
)
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot

# Only these items are pushed (everything else stays Pi-side).
$items = @(
    "src", "deploy",
    "capture.py", "detect_preview.py",
    "config.yaml", "config.pi.yaml",
    "requirements.txt", "README.md"
)

Write-Host "Syncing to ${Pi}:${Dest} ..." -ForegroundColor Cyan
foreach ($i in $items) {
    $path = Join-Path $src $i
    if (Test-Path $path) {
        scp -r -q $path "${Pi}:$Dest/"
        Write-Host "  + $i"
    }
}
# Strip Windows-side __pycache__ that may have tagged along, and fix perms.
ssh $Pi "find $Dest -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null; chmod -R u+rwX $Dest"
Write-Host "Done." -ForegroundColor Green
