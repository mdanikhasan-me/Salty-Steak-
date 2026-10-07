param(
    [string]$EvidenceDirectory = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([string]::IsNullOrWhiteSpace($EvidenceDirectory)) {
    $EvidenceDirectory = Join-Path $projectRoot "validation\ui-runtime-hardening-r29-20260810"
} else {
    $EvidenceDirectory = [IO.Path]::GetFullPath($EvidenceDirectory)
}
$projectPrefix = $projectRoot.TrimEnd("\") + "\"
$targets = [System.Collections.Generic.List[object]]::new()
$registeredModelArtifacts = [System.Collections.Generic.List[object]]::new()

# Models are not cleanup targets. Read their own manifests so the final record
# proves the actual registered files that survived, instead of checking a
# retired hard-coded filename.
$modelRoot = Join-Path $projectRoot "workspace\models"
if (Test-Path -LiteralPath $modelRoot) {
    $resolvedModelRoot = (Resolve-Path -LiteralPath $modelRoot).Path.TrimEnd("\") + "\"
    Get-ChildItem -LiteralPath $modelRoot -Recurse -File -Filter "model.json" -ErrorAction Stop |
        ForEach-Object {
            try {
                $manifest = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                $filename = [string]$manifest.artifact.filename
            } catch {
                throw "Unable to read model manifest: $($_.FullName)"
            }
            if ([string]::IsNullOrWhiteSpace($filename) -or [IO.Path]::GetFileName($filename) -ne $filename) {
                throw "Model manifest has an unsafe artifact filename: $($_.FullName)"
            }
            $artifact = [IO.Path]::GetFullPath((Join-Path $_.DirectoryName $filename))
            if (-not $artifact.StartsWith($resolvedModelRoot, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Model artifact escaped workspace/models: $artifact"
            }
            $registeredModelArtifacts.Add([pscustomobject]@{
                manifest = $_.FullName
                artifact = $artifact
            })
        }
}

function Add-CleanupTarget([string]$Path, [string]$Reason) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if (-not $resolved.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Cleanup target escaped the project root: $resolved"
    }
    $item = Get-Item -LiteralPath $resolved -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Cleanup refuses a reparse-point target: $resolved"
    }
    $targets.Add([pscustomobject]@{ Path = $resolved; Reason = $Reason; Item = $item })
}

Get-ChildItem -LiteralPath $projectRoot -Directory -Force |
    Where-Object { $_.Name -match '^workspace-r(18|19)(-|$)' } |
    ForEach-Object { Add-CleanupTarget $_.FullName "retired isolated preview workspace" }

Add-CleanupTarget (Join-Path $projectRoot ".pytest_cache") "pytest cache"
Add-CleanupTarget (Join-Path $projectRoot "app\frontend\dist") "regenerable frontend build"
Add-CleanupTarget (Join-Path $projectRoot "app\desktop\native\bin") "regenerable .NET build output"
Add-CleanupTarget (Join-Path $projectRoot "app\desktop\native\obj") "regenerable .NET intermediate output"
Add-CleanupTarget (Join-Path $projectRoot "check_abi.obj") "retired ABI probe output"

foreach ($sourceRoot in @("app", "tests", "tools")) {
    Get-ChildItem -LiteralPath (Join-Path $projectRoot $sourceRoot) -Directory -Recurse -Force `
        -Filter "__pycache__" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.FullName -notlike "*\app\desktop\native\bin\*" -and
            $_.FullName -notlike "*\app\desktop\native\obj\*"
        } |
        ForEach-Object { Add-CleanupTarget $_.FullName "Python bytecode cache" }
}

Get-ChildItem -LiteralPath (Join-Path $projectRoot "workspace\cache") -Directory -Force `
    -Filter "desktop-publish-*" -ErrorAction SilentlyContinue |
    ForEach-Object { Add-CleanupTarget $_.FullName "leaked temporary desktop publish" }

$unique = $targets |
    Group-Object { $_.Path.ToLowerInvariant() } |
    ForEach-Object { $_.Group[0] } |
    Sort-Object { $_.Path.Length } -Descending
$removed = @()
$removedFileCount = [int64]0
$removedBytes = [int64]0
foreach ($target in $unique) {
    $item = $target.Item
    if ($item.PSIsContainer) {
        $measurement = Get-ChildItem -LiteralPath $target.Path -Recurse -Force -File `
            -ErrorAction SilentlyContinue | Measure-Object Length -Sum
        $files = $measurement.Count
        $bytes = [int64]$measurement.Sum
        Remove-Item -LiteralPath $target.Path -Recurse -Force
    } else {
        $files = 1
        $bytes = [int64]$item.Length
        Remove-Item -LiteralPath $target.Path -Force
    }
    $removedFileCount += [int64]$files
    $removedBytes += [int64]$bytes
    $removed += [ordered]@{
        path = $target.Path
        reason = $target.Reason
        files = $files
        bytes = $bytes
    }
}

$result = [ordered]@{
    success = $true
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    project_root = $projectRoot
    removed_target_count = $removed.Count
    removed_file_count = $removedFileCount
    removed_bytes = $removedBytes
    postconditions = [ordered]@{
        retired_preview_workspaces = @(
            Get-ChildItem -LiteralPath $projectRoot -Directory -Force |
                Where-Object { $_.Name -match '^workspace-r(18|19)(-|$)' }
        ).Count
        leaked_desktop_publish_directories = @(
            Get-ChildItem -LiteralPath (Join-Path $projectRoot "workspace\cache") `
                -Directory -Force -Filter "desktop-publish-*" -ErrorAction SilentlyContinue
        ).Count
        source_python_cache_directories = @(
            foreach ($sourceRoot in @("app", "tests", "tools")) {
                Get-ChildItem -LiteralPath (Join-Path $projectRoot $sourceRoot) `
                    -Directory -Recurse -Force -Filter "__pycache__" `
                    -ErrorAction SilentlyContinue
            }
        ).Count
        old_train_page_exists = Test-Path -LiteralPath (
            Join-Path $projectRoot "app\frontend\src\pages\TrainPage.jsx"
        )
        old_versions_page_exists = Test-Path -LiteralPath (
            Join-Path $projectRoot "app\frontend\src\pages\VersionsPage.jsx"
        )
        model_bundle_artifacts = @(
            $registeredModelArtifacts | ForEach-Object {
                [ordered]@{
                    manifest = $_.manifest
                    artifact = $_.artifact
                    exists = Test-Path -LiteralPath $_.artifact -PathType Leaf
                }
            }
        )
    }
    preserved = @(
        "workspace/models and model weights"
        "workspace/runtime and runtime-cache"
        ".venv, node_modules, and vendor dependencies"
        "dist release candidates and validation evidence"
    )
    removed = $removed
}
New-Item -ItemType Directory -Path $EvidenceDirectory -Force | Out-Null
$resultPath = Join-Path $EvidenceDirectory "GENERATED_CACHE_CLEANUP.json"
$temporary = $resultPath + ".tmp"
[IO.File]::WriteAllText(
    $temporary,
    ($result | ConvertTo-Json -Depth 6),
    [Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $temporary -Destination $resultPath -Force
$result | ConvertTo-Json -Depth 6
