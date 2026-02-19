param(
  [Parameter(Mandatory = $true)]
  [string]$RunId,
  [string]$StorageRoot = "config\\runs"
)

$ErrorActionPreference = "Stop"

try {
  $raw = & python -m council_os.cli checkpoints --run-id $RunId --storage-root $StorageRoot
} catch {
  Write-Host "Failed to read checkpoints for run $RunId from $StorageRoot."
  exit 1
}

if (-not $raw) {
  Write-Host "No checkpoints found for run $RunId."
  exit 1
}

try {
  $items = $raw | ConvertFrom-Json
} catch {
  Write-Host "Failed to parse checkpoints JSON."
  exit 1
}

if (-not $items) {
  Write-Host "No checkpoints found for run $RunId."
  exit 1
}

$latest = $items | Sort-Object created_at -Descending | Select-Object -First 1
$match = $items | Where-Object { $_.stage_name -eq "judge.pairwise" } |
  Sort-Object created_at -Descending |
  Select-Object -First 1

if ($match) {
  Write-Host "OK: judge.pairwise checkpoint found."
  Write-Host ("checkpoint_id: {0}" -f $match.checkpoint_id)
  Write-Host ("created_at:   {0}" -f $match.created_at)
  if ($latest) {
    Write-Host ("latest_stage: {0} ({1})" -f $latest.stage_name, $latest.created_at)
  }
  exit 0
}

Write-Host "MISSING: no judge.pairwise checkpoint found."
if ($latest) {
  Write-Host ("latest_stage: {0} ({1})" -f $latest.stage_name, $latest.created_at)
}
exit 2
