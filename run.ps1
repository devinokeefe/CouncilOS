param(
  [string]$Config = "config/llm_openai_anthropic.yml",
  [string]$Brief = "eval/golden_briefs/brief_01.md",
  [string]$RunId = "",
  [string]$CheckpointId = "",
  [switch]$Resume
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$storageRoot = @'
import sys, pathlib, yaml
config_path = pathlib.Path(sys.argv[1])
raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
runtime = raw.get("runtime", {}) if isinstance(raw, dict) else {}
artifacts = runtime.get("artifacts", {}) if isinstance(runtime, dict) else {}
store_dir = artifacts.get("store_dir") if isinstance(artifacts, dict) else None
if isinstance(store_dir, str) and store_dir:
    base = store_dir.replace("{{run_id}}", "").replace("{run_id}", "").rstrip("/\\")
    if base.endswith("artifacts"):
        base = str(pathlib.Path(base).parent)
    base_path = pathlib.Path(base) if base else pathlib.Path(".")
    if not base_path.is_absolute():
        base_path = (config_path.parent / base_path).resolve()
    print(base_path)
else:
    print(raw.get("storage_root", "CouncilOS/runs"))
'@ | python - $Config
$storageRoot = $storageRoot.Trim()

$shouldResume = $Resume.IsPresent -or ($RunId -ne "")
if ($shouldResume) {
  if (-not $RunId) {
    $latest = Get-ChildItem -Directory -Path $storageRoot | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) {
      throw "No runs found under $storageRoot"
    }
    $RunId = $latest.Name
  }
  if ($CheckpointId) {
    python -m council_os.cli resume --run-id $RunId --checkpoint-id $CheckpointId --storage-root $storageRoot
  } else {
    python -m council_os.cli resume --run-id $RunId --storage-root $storageRoot
  }
} else {
  python -m council_os.cli run --config $Config --brief $Brief
}
