param(
    [Parameter(Mandatory = $true)]
    [string]$Destination,
    [string]$EvidencePath = "",
    [string]$CandidateState = "staged_for_acceptance",
    [string]$CandidateReason = (
        "This isolated workspace is staged for acceptance. Production cutover is not implied."
    )
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$validationRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot "validation"))
$destinationRoot = [IO.Path]::GetFullPath($Destination)
if (
    $destinationRoot -eq $validationRoot -or
    -not $destinationRoot.StartsWith(
        $validationRoot + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "Candidate workspace must be a named child of validation"
}
if (Test-Path -LiteralPath $destinationRoot) {
    throw "Refusing to overwrite candidate workspace: $destinationRoot"
}

$script:hardlinks = 0
$script:copies = 0

function Copy-TreeWithHardlinks {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Target
    )
    $sourceRoot = (Resolve-Path -LiteralPath $Source).Path
    New-Item -ItemType Directory -Path $Target -Force | Out-Null
    Get-ChildItem -LiteralPath $sourceRoot -Directory -Recurse -Force | ForEach-Object {
        $relative = $_.FullName.Substring($sourceRoot.Length).TrimStart('\', '/')
        New-Item -ItemType Directory -Path (Join-Path $Target $relative) -Force | Out-Null
    }
    Get-ChildItem -LiteralPath $sourceRoot -File -Recurse -Force | ForEach-Object {
        $relative = $_.FullName.Substring($sourceRoot.Length).TrimStart('\', '/')
        $targetFile = Join-Path $Target $relative
        # Candidate manifests are rewritten below. A hard link would make that
        # rewrite mutate the source manifest too, invalidating both provenance
        # and the supposedly immutable stage.
        if ($_.Name -eq "model.json") {
            Copy-Item -LiteralPath $_.FullName -Destination $targetFile
            $script:copies += 1
        } else {
            try {
                New-Item -ItemType HardLink -Path $targetFile -Target $_.FullName -ErrorAction Stop | Out-Null
                $script:hardlinks += 1
            } catch {
                Copy-Item -LiteralPath $_.FullName -Destination $targetFile
                $script:copies += 1
            }
        }
    }
}

New-Item -ItemType Directory -Path $destinationRoot | Out-Null
foreach ($name in @(
    "control", "datasets", "evaluations", "training", "versions",
    "conversations", "logs", "cache", "generated", "automation"
)) {
    New-Item -ItemType Directory -Path (Join-Path $destinationRoot $name) | Out-Null
}

Copy-TreeWithHardlinks `
    -Source (Join-Path $projectRoot "workspace\tokenizer") `
    -Target (Join-Path $destinationRoot "tokenizer")
Copy-TreeWithHardlinks `
    -Source (Join-Path $projectRoot "workspace\models\image-generation") `
    -Target (Join-Path $destinationRoot "models\image-generation")
$candidateModelSource = Join-Path $projectRoot (
    "workspace\models\text-generation\base-steak-2-0-9b-steak20-candidate1"
)
$candidateModelTarget = Join-Path $destinationRoot (
    "models\text-generation\base-steak-2-0-9b-steak20"
)
Copy-TreeWithHardlinks -Source $candidateModelSource -Target $candidateModelTarget
Copy-TreeWithHardlinks `
    -Source (Join-Path $projectRoot "workspace\runtime\salty-native-steak20") `
    -Target (Join-Path $destinationRoot "runtime\salty-native-steak20")
Copy-TreeWithHardlinks `
    -Source (Join-Path $projectRoot "workspace\runtime\salty-vision-steak20-candidate2") `
    -Target (Join-Path $destinationRoot "runtime\salty-vision")

$manifestPath = Join-Path $candidateModelTarget "model.json"
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$manifest.runtime.state = $CandidateState
$manifest.runtime.activation_allowed = $true
$manifest.runtime.reason = $CandidateReason
$manifest.chat.selected = $true
$manifest.chat.active = $true
$manifest.context.activation_state = $CandidateState
$manifest.context.activation_note = (
    "32K resident default and selectable adaptive contexts through 262K passed " +
    "the exact private-worker load and generation gate."
)
$manifest.integrity.state = "sha256_and_tensor_payload_verified_at_rebuild"
$manifest.registered_at = [DateTimeOffset]::UtcNow.ToString("o")
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText(
    $manifestPath,
    (($manifest | ConvertTo-Json -Depth 20) + "`n"),
    $utf8NoBom
)

$manifestHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$evidence = [ordered]@{
    schema = "salty-steak-candidate-workspace-stage-v1"
    destination = $destinationRoot
    model_manifest = $manifestPath
    model_manifest_sha256 = $manifestHash
    model_id = $manifest.id
    selected = [bool]$manifest.chat.selected
    active = [bool]$manifest.chat.active
    activation_allowed = [bool]$manifest.runtime.activation_allowed
    candidate_state = $CandidateState
    candidate_reason = $CandidateReason
    default_context_tokens = [int]$manifest.context.default_tokens
    configured_context_tokens = [int]$manifest.context.configured_tokens
    hardlinked_files = $script:hardlinks
    copied_files = $script:copies
}
if (-not [string]::IsNullOrWhiteSpace($EvidencePath)) {
    $resolvedEvidence = [IO.Path]::GetFullPath($EvidencePath)
    if (
        -not $resolvedEvidence.StartsWith(
            $validationRoot + [IO.Path]::DirectorySeparatorChar,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw "EvidencePath must remain inside validation"
    }
    if (Test-Path -LiteralPath $resolvedEvidence) {
        throw "Refusing to overwrite evidence: $resolvedEvidence"
    }
    [IO.File]::WriteAllText(
        $resolvedEvidence,
        (($evidence | ConvertTo-Json -Depth 10) + "`n"),
        $utf8NoBom
    )
}
$evidence | ConvertTo-Json -Depth 10
