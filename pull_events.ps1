# pull_events.ps1 — copy event snapshots from the Pi to this laptop, then delete
# them from the Pi (so its SD stays nearly empty). The laptop PULLS (outbound SSH),
# which works through corporate firewalls; the Pi never has to reach the laptop.
#
#   .\pull_events.ps1                       # one pass into Desktop\counter_events
#   .\pull_events.ps1 -Loop -Every 30       # pull every 30s until Ctrl+C
#   .\pull_events.ps1 -Dest "D:\events"     # custom destination folder
#
param(
    [string]$Pi    = "pi-cam",
    [string]$Dest  = "C:\counter_events",
    [switch]$Loop,
    [int]$Every    = 30
)
$ErrorActionPreference = "Continue"
$remote = "~/counter_vision/data/events"
New-Item -ItemType Directory -Force $Dest | Out-Null

function Invoke-PullOnce {
    $list = (ssh $Pi "ls $remote/*.jpg 2>/dev/null") -split "`n" | Where-Object { $_ }
    if (-not $list) { Write-Host ("{0}  no new snapshots" -f (Get-Date -Format HH:mm:ss)); return }
    scp -q "${Pi}:$remote/*.jpg" "$Dest\" 2>$null
    ssh $Pi ("rm -f " + ($list -join " "))   # delete only what we listed; new arrivals stay for next pass
    Write-Host ("{0}  pulled {1} -> {2}" -f (Get-Date -Format HH:mm:ss), $list.Count, $Dest)
}

if ($Loop) {
    Write-Host "Pulling every ${Every}s into $Dest  (Ctrl+C to stop)"
    while ($true) { Invoke-PullOnce; Start-Sleep -Seconds $Every }
} else {
    Invoke-PullOnce
}
