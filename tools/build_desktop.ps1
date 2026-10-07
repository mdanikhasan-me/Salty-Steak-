param(
    [string]$Configuration = "Release",
    [string]$BuildId = "2.0.0+20260726.native-r2",
    [string]$PackageDirectory = "",
    [string]$SigningThumbprint = "",
    [string]$SignToolPath = "",
    [string[]]$EvidenceFiles = @(),
    [string]$ProtectedArtifactFingerprint = "",
    [string]$SourceBackupSha256 = "",
    [string]$SourceManifestPath = "",
    [string]$ExpectedSourceTreeSha256 = "",
    [int]$EvaluationBatchSize = 32,
    [int]$TrainingMicroBatch = 16,
    [int]$TrainingGradientAccumulation = 1,
    [int]$TrainingEffectiveBatch = 16,
    [int]$TrainingSequenceLength = 512,
    [string]$TrainingPrecision = "bf16"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$sourceSnapshot = $null
$resolvedSourceManifest = $null
if (-not [string]::IsNullOrWhiteSpace($SourceManifestPath)) {
    $resolvedSourceManifest = (Resolve-Path -LiteralPath $SourceManifestPath).Path
    $snapshotOutput = & (Join-Path $projectRoot ".venv\Scripts\python.exe") `
        (Join-Path $projectRoot "tools\snapshot_release_source.py") `
        $projectRoot --verify $resolvedSourceManifest | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Release source differs from the frozen source manifest before build."
    }
    $sourceSnapshot = $snapshotOutput | ConvertFrom-Json
    if (
        -not [string]::IsNullOrWhiteSpace($ExpectedSourceTreeSha256) -and
        $sourceSnapshot.source_tree_sha256 -ne $ExpectedSourceTreeSha256.ToLowerInvariant()
    ) {
        throw "Frozen source tree digest does not match the expected digest."
    }
}
$projectFile = Join-Path $projectRoot "app\desktop\native\SaltyPotatoAI.csproj"
$publishDirectory = Join-Path $projectRoot (
    "workspace\cache\desktop-publish-" + [Guid]::NewGuid().ToString("N")
)
if ([string]::IsNullOrWhiteSpace($PackageDirectory)) {
    $packageDirectory = Join-Path $projectRoot "dist\Salty-Steak-native-r16"
} else {
    $packageDirectory = [IO.Path]::GetFullPath($PackageDirectory)
}

try {
$previousFrontendBuildId = $env:SALTY_POTATO_BUILD_ID
$env:SALTY_POTATO_BUILD_ID = $BuildId
$previousNativeErrorPreference = $ErrorActionPreference
$frontendExitCode = $null
try {
    # Windows PowerShell wraps native stderr (including Vite size warnings) as
    # ErrorRecords. Do not mistake a warning for build failure; preserve output
    # and decide success using the actual native exit code. Resolve first so a
    # missing npm is still a terminating error.
    $npmCommand = (Get-Command npm.cmd -ErrorAction Stop).Source
    $ErrorActionPreference = 'Continue'
    & $npmCommand run build
    $frontendExitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousNativeErrorPreference
    $env:SALTY_POTATO_BUILD_ID = $previousFrontendBuildId
}
if ($null -eq $frontendExitCode -or $frontendExitCode -ne 0) {
    throw "The frontend production build failed."
}

$publishArguments = @(
    "publish"
    $projectFile
    "--configuration", $Configuration
    "--runtime", "win-x64"
    "--output", $publishDirectory
    "-p:InformationalVersion=$BuildId"
    # The .NET SDK appends SourceRevisionId to InformationalVersion by default.
    # BuildId already is the complete cross-layer identity; allowing that suffix
    # makes the executable disagree with package.json and the frontend marker.
    "-p:IncludeSourceRevisionInInformationalVersion=false"
    "--nologo"
)
$assetsFile = Join-Path $projectRoot "app\desktop\native\obj\project.assets.json"
if (
    (Test-Path -LiteralPath $assetsFile -PathType Leaf) -and
    (Get-Item -LiteralPath $assetsFile).LastWriteTimeUtc -ge
        (Get-Item -LiteralPath $projectFile).LastWriteTimeUtc
) {
    $publishArguments += "--no-restore"
}
& dotnet @publishArguments
if ($LASTEXITCODE -ne 0) {
    throw "The native desktop publish failed."
}

$publishedExecutable = Join-Path $publishDirectory "Salty Steak.exe"
if (-not (Test-Path -LiteralPath $publishedExecutable -PathType Leaf)) {
    throw "The desktop build did not produce Salty Steak.exe."
}
if ($null -ne $sourceSnapshot) {
    $snapshotOutput = & (Join-Path $projectRoot ".venv\Scripts\python.exe") `
        (Join-Path $projectRoot "tools\snapshot_release_source.py") `
        $projectRoot --verify $resolvedSourceManifest | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "Release source changed while frontend/native outputs were built."
    }
    $verifiedAfterBuild = $snapshotOutput | ConvertFrom-Json
    if (
        $verifiedAfterBuild.source_tree_sha256 -ne
        $sourceSnapshot.source_tree_sha256
    ) {
        throw "Release source tree digest changed during the build."
    }
}

$unsignedExecutableHash = (
    Get-FileHash -LiteralPath $publishedExecutable -Algorithm SHA256
).Hash.ToLowerInvariant()
$certificate = $null
$signatureStatus = "NotSigned"
$signedExecutableHash = $unsignedExecutableHash
if (-not [string]::IsNullOrWhiteSpace($SigningThumbprint)) {
    $certificate = Get-ChildItem -LiteralPath "Cert:\CurrentUser\My\$SigningThumbprint" `
        -ErrorAction SilentlyContinue
    if (
        $null -eq $certificate `
        -or -not $certificate.HasPrivateKey `
        -or -not ($certificate.EnhancedKeyUsageList.ObjectId -contains "1.3.6.1.5.5.7.3.3") `
        -or $certificate.Subject -eq $certificate.Issuer
    ) {
        throw (
            "The certificate is unavailable, self-signed, lacks a private key, or cannot sign code: " +
            $SigningThumbprint
        )
    }
    if ([string]::IsNullOrWhiteSpace($SignToolPath)) {
        $kitsRoot = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin"
        $signToolPath = Get-ChildItem -LiteralPath $kitsRoot -Filter "signtool.exe" -Recurse `
            -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\signtool\.exe$" } |
            Sort-Object { [version]($_.Directory.Parent.Name) } -Descending |
            Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not (Test-Path -LiteralPath $signToolPath -PathType Leaf)) {
        throw "Windows SignTool could not be found."
    }
    & $signToolPath sign /sha1 $SigningThumbprint /fd SHA256 $publishedExecutable
    if ($LASTEXITCODE -ne 0) {
        throw "Signing the desktop executable failed."
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $publishedExecutable
    if (
        $signature.Status -ne "Valid" `
        -or $signature.SignerCertificate.Thumbprint -ne $SigningThumbprint
    ) {
        throw "The signed executable did not validate against the approved certificate."
    }
    $signatureStatus = $signature.Status.ToString()
    $signedExecutableHash = (
        Get-FileHash -LiteralPath $publishedExecutable -Algorithm SHA256
    ).Hash.ToLowerInvariant()
    if ($signedExecutableHash -eq $unsignedExecutableHash) {
        throw "Signing did not change the executable hash."
    }
}

if (Test-Path -LiteralPath $packageDirectory) {
    throw "Refusing to mix files into an existing package: $packageDirectory"
}

New-Item -ItemType Directory -Path $packageDirectory | Out-Null
Copy-Item -LiteralPath $publishedExecutable -Destination (Join-Path $packageDirectory "Salty Steak.exe")
# The UI-automation and browser rungs are separate helper processes. A package
# without them reports those capabilities as unavailable rather than failing at
# the first call, so they are part of the package, not an afterthought.
# Each helper is copied whole: the bare executable cannot start without its
# managed assembly, runtimeconfig and native loader beside it. The broker looks
# for <package>\uia\ and <package>\browser\, so those are the folder names.
foreach ($helper in @(
    @{ Name = "SaltyUiaHost.exe"; Project = "app\desktop\uia"; Folder = "uia" },
    @{ Name = "SaltyBrowserHost.exe"; Project = "app\desktop\browser"; Folder = "browser" }
)) {
    # Build the frozen helper sources on every release. Existing bin outputs
    # can belong to an older checkout and are not release provenance.
    $helperDirectory = Join-Path $publishDirectory ("helper-" + $helper.Folder)
    & dotnet build (Join-Path $projectRoot $helper.Project) `
        --configuration Release --runtime win-x64 --nologo --output $helperDirectory
    if ($LASTEXITCODE -ne 0) {
        throw ("Building the helper host failed: " + $helper.Name)
    }
    $helperSource = Join-Path $helperDirectory $helper.Name
    if (-not (Test-Path -LiteralPath $helperSource -PathType Leaf)) {
        throw ("The helper host was not produced: " + $helper.Name)
    }
    Copy-Item -LiteralPath $helperDirectory `
        -Destination (Join-Path $packageDirectory $helper.Folder) -Recurse
}
Copy-Item -LiteralPath (Join-Path $projectRoot "config") -Destination (Join-Path $packageDirectory "config") -Recurse
Remove-Item -LiteralPath (Join-Path $packageDirectory "config\local.toml") -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path (Join-Path $packageDirectory "app\frontend") -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot "app\__init__.py") -Destination (Join-Path $packageDirectory "app\__init__.py")
$backendSource = Join-Path $projectRoot "app\backend"
$backendDestination = Join-Path $packageDirectory "app\backend"
& robocopy $backendSource $backendDestination /E /COPY:DAT /DCOPY:DAT /MT:32 /R:1 /W:1 `
    /XD "__pycache__" /XF "*.pyc" /NFL /NDL /NJH /NJS /NP | Out-Host
if ($LASTEXITCODE -gt 7) {
    throw "Could not stage the backend without Python bytecode (robocopy exit code $LASTEXITCODE)."
}
Copy-Item -LiteralPath (Join-Path $projectRoot "app\frontend\dist") -Destination (Join-Path $packageDirectory "app\frontend\dist") -Recurse
Copy-Item -LiteralPath (Join-Path $projectRoot "app\frontend\public") -Destination (Join-Path $packageDirectory "app\frontend\public") -Recurse
$venvSource = Join-Path $projectRoot ".venv"
$venvDestination = Join-Path $packageDirectory ".venv"
& robocopy $venvSource $venvDestination /E /COPY:DAT /DCOPY:DAT /MT:32 /R:1 /W:1 `
    /XD "__pycache__" "include" "test" "tests" /NFL /NDL /NJH /NJS /NP | Out-Host
if ($LASTEXITCODE -gt 7) {
    throw "Could not stage the private application runtime (robocopy exit code $LASTEXITCODE)."
}
$sourcePythonHome = (
    Get-Content -LiteralPath (Join-Path $venvSource "pyvenv.cfg") |
        Where-Object { $_ -match "^home\s*=" } |
        Select-Object -First 1
).Split("=", 2)[1].Trim()
if (-not (Test-Path -LiteralPath (Join-Path $sourcePythonHome "python312.dll") -PathType Leaf)) {
    throw "The base Python runtime required by the private interpreter is incomplete."
}
$pythonDestination = Join-Path $packageDirectory ".python"
& robocopy $sourcePythonHome $pythonDestination /E /COPY:DAT /DCOPY:DAT /MT:32 /R:1 /W:1 `
    /XD "__pycache__" "include" "test" "tests" /NFL /NDL /NJH /NJS /NP | Out-Host
if ($LASTEXITCODE -gt 7) {
    throw "Could not stage the base Python runtime (robocopy exit code $LASTEXITCODE)."
}
$packagedPython = Join-Path $pythonDestination "python.exe"
@(
    # Keep the package relocatable across candidate, staging, live, and
    # rollback directories. Windows' venv launcher resolves this relative
    # home from the explicit package working directory used by every child.
    "home = .python"
    "include-system-site-packages = false"
    "version = 3.12.10"
    "executable = .python\python.exe"
    "command = .python\python.exe -m venv .venv"
) | Set-Content -LiteralPath (Join-Path $venvDestination "pyvenv.cfg") -Encoding utf8
$privatePythonVersion = (& $packagedPython --version 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($privatePythonVersion)) {
    throw "The packaged private Python runtime did not report its identity."
}

$evidenceEntries = @()
if ($EvidenceFiles.Count -gt 0) {
    $evidenceDirectory = Join-Path $packageDirectory "evidence"
    New-Item -ItemType Directory -Path $evidenceDirectory -Force | Out-Null
    $seenEvidenceNames = @{}
    foreach ($evidenceFile in $EvidenceFiles) {
        $resolvedEvidence = (Resolve-Path -LiteralPath $evidenceFile).Path
        $name = [IO.Path]::GetFileName($resolvedEvidence)
        if ($seenEvidenceNames.ContainsKey($name)) {
            throw "Evidence file names must be unique inside the sealed package: $name"
        }
        $seenEvidenceNames[$name] = $true
        $packagedEvidence = Join-Path $evidenceDirectory $name
        Copy-Item -LiteralPath $resolvedEvidence -Destination $packagedEvidence
        $evidenceEntries += [ordered]@{
            source_path = $resolvedEvidence
            packaged_path = "evidence/$name"
            bytes = (Get-Item -LiteralPath $packagedEvidence).Length
            sha256 = (
                Get-FileHash -LiteralPath $packagedEvidence -Algorithm SHA256
            ).Hash.ToLowerInvariant()
        }
    }
}

$forbiddenBytecode = @(
    Get-ChildItem -LiteralPath $packageDirectory -Recurse -Force |
        Where-Object {
            $_.Name -eq "__pycache__" -or
            (-not $_.PSIsContainer -and $_.Extension -eq ".pyc")
        }
)
if ($forbiddenBytecode.Count -ne 0) {
    throw (
        "The staged package contains forbidden Python bytecode: " +
        ($forbiddenBytecode[0].FullName)
    )
}

$packageRecord = [ordered]@{
    product = "Salty Steak"
    build_id = $BuildId
    native_host_version = (Get-Item -LiteralPath $publishedExecutable).VersionInfo.ProductVersion
    frontend_build_id = $BuildId
    packaged_at_utc = [DateTime]::UtcNow.ToString("o")
    executable = "Salty Steak.exe"
    companion_folders = @(
        "app",
        "config",
        ".venv",
        ".python"
    ) + $(if ($evidenceEntries.Count -gt 0) { @("evidence") } else { @() })
    workspace = "%LOCALAPPDATA%\Salty Steak\Workspace"
    source_dependency = $false
    repository_isolation = $true
    workspace_isolation = $true
    private_python = [ordered]@{
        version = $privatePythonVersion
        executable = ".python/python.exe"
        executable_sha256 = (
            Get-FileHash -LiteralPath $packagedPython -Algorithm SHA256
        ).Hash.ToLowerInvariant()
    }
    performance_profile = [ordered]@{
        training_micro_batch = $TrainingMicroBatch
        training_gradient_accumulation = $TrainingGradientAccumulation
        training_effective_batch = $TrainingEffectiveBatch
        training_sequence_length = $TrainingSequenceLength
        training_precision = $TrainingPrecision
        evaluation_batch_size = $EvaluationBatchSize
    }
    evidence = @($evidenceEntries)
    protected_artifact_fingerprint = $ProtectedArtifactFingerprint
    source_backup_sha256 = $SourceBackupSha256
    source_manifest_sha256 = if ($null -ne $resolvedSourceManifest) {
        (
            Get-FileHash -LiteralPath $resolvedSourceManifest -Algorithm SHA256
        ).Hash.ToLowerInvariant()
    } else { "" }
    source_tree_sha256 = if ($null -ne $sourceSnapshot) {
        $sourceSnapshot.source_tree_sha256
    } else { "" }
    source_snapshot_verified_before_after = ($null -ne $sourceSnapshot)
    signing = [ordered]@{
        certificate_subject = if ($null -ne $certificate) { $certificate.Subject } else { $null }
        certificate_thumbprint = if ($null -ne $certificate) { $certificate.Thumbprint } else { $null }
        signature_status = $signatureStatus
        unsigned_executable_sha256 = $unsignedExecutableHash
        signed_executable_sha256 = $signedExecutableHash
        timestamped = $false
    }
}
$packageRecord | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath (Join-Path $packageDirectory "package.json") -Encoding utf8

$integrityEntries = Get-ChildItem -LiteralPath $packageDirectory -File -Recurse |
    Where-Object { $_.Name -ne "integrity-manifest.json" } |
    Sort-Object FullName |
    ForEach-Object {
        $relative = $_.FullName.Substring($packageDirectory.Length + 1).Replace("\", "/")
        [ordered]@{
            path = $relative
            bytes = $_.Length
            sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
[ordered]@{
    product = "Salty Steak"
    build_id = $BuildId
    algorithm = "SHA-256"
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
    files = @($integrityEntries)
} | ConvertTo-Json -Depth 6 |
    Set-Content -LiteralPath (Join-Path $packageDirectory "integrity-manifest.json") -Encoding utf8

Write-Host "Packaged $packageDirectory"
} finally {
    if (Test-Path -LiteralPath $publishDirectory) {
        $resolvedPublish = (Resolve-Path -LiteralPath $publishDirectory).Path
        $resolvedCache = (Resolve-Path -LiteralPath (Join-Path $projectRoot 'workspace\cache')).Path.TrimEnd('\') + '\'
        $publishItem = Get-Item -LiteralPath $resolvedPublish
        if (-not $resolvedPublish.StartsWith($resolvedCache, [StringComparison]::OrdinalIgnoreCase) -or
            $publishItem.Name -notmatch '^desktop-publish-[a-f0-9]{32}$' -or
            ($publishItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Refusing cleanup of an unexpected native publish directory.'
        }
        Remove-Item -LiteralPath $resolvedPublish -Recurse -Force
    }
}
