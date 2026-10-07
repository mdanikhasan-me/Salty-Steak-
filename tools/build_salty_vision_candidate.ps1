param(
    [string]$SourceRoot = "D:\Salty Steak\workspace\cache\salty-native-src-b10333",
    [string]$BuildRoot = "D:\Salty Steak\workspace\cache\native-engine-build\salty-vision-steak20-b10333",
    [string]$CandidateBin = "D:\Salty Steak\workspace\cache\native-engine-build\salty-vision-steak20-b10333-candidate1\bin",
    [string]$TextRuntimeBin = "D:\Salty Steak\workspace\runtime\salty-native-steak20\bin"
)

$ErrorActionPreference = "Stop"

$allowedBuildRoot = [IO.Path]::GetFullPath(
    "D:\Salty Steak\workspace\cache\native-engine-build"
)
$resolvedBuildRoot = [IO.Path]::GetFullPath($BuildRoot)
$resolvedCandidateBin = [IO.Path]::GetFullPath($CandidateBin)
if (-not $resolvedBuildRoot.StartsWith($allowedBuildRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "BuildRoot must remain inside the app-owned native build cache"
}
if (-not $resolvedCandidateBin.StartsWith($allowedBuildRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw "CandidateBin must remain inside the app-owned native build cache"
}

$cmakeCandidates = @(
    "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
    "C:\Program Files\CodeBlocks\MinGW\bin\cmake.exe"
)
$cmake = $cmakeCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $cmake) {
    throw "CMake was not found in the supported local build-tool locations"
}
if (-not (Test-Path -LiteralPath $SourceRoot -PathType Container)) {
    throw "The frozen engine source tree is missing"
}

& $cmake `
    -S $SourceRoot `
    -B $resolvedBuildRoot `
    -G "Visual Studio 18 2026" `
    -A x64 `
    -DBUILD_SHARED_LIBS=ON `
    -DGGML_CUDA=OFF `
    -DGGML_NATIVE=OFF `
    -DLLAMA_BUILD_TESTS=OFF `
    -DLLAMA_BUILD_EXAMPLES=OFF `
    -DLLAMA_BUILD_SERVER=OFF `
    -DLLAMA_BUILD_TOOLS=ON `
    -DLLAMA_BUILD_COMMON=ON `
    -DLLAMA_BUILD_APP=OFF `
    -DLLAMA_BUILD_MTMD=OFF `
    -DMTMD_VIDEO=OFF
if ($LASTEXITCODE -ne 0) {
    throw "The isolated vision candidate could not be configured"
}

& $cmake --build $resolvedBuildRoot --config Release --target llama-mtmd-cli --parallel 12
if ($LASTEXITCODE -ne 0) {
    throw "The isolated vision candidate could not be built"
}

$builtBin = Join-Path $resolvedBuildRoot "bin\Release"
New-Item -ItemType Directory -Path $resolvedCandidateBin -Force | Out-Null
foreach ($name in @(
    "ggml.dll",
    "ggml-base.dll",
    "ggml-cpu.dll",
    "llama.dll",
    "llama-common.dll",
    "mtmd.dll",
    "llama-mtmd-cli.exe"
)) {
    Copy-Item -LiteralPath (Join-Path $builtBin $name) -Destination (Join-Path $resolvedCandidateBin $name) -Force
}
foreach ($name in @(
    "ggml-cuda.dll",
    "cudart64_12.dll",
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "libomp140.x86_64.dll"
)) {
    Copy-Item -LiteralPath (Join-Path $TextRuntimeBin $name) -Destination (Join-Path $resolvedCandidateBin $name) -Force
}

$deviceOutput = & (Join-Path $resolvedCandidateBin "llama-mtmd-cli.exe") --list-devices --offline
$deviceOutputText = $deviceOutput -join "`n"
if ($LASTEXITCODE -ne 0 -or $deviceOutputText -notmatch "CUDA0") {
    throw "The isolated candidate did not discover the validated CUDA backend"
}

Get-ChildItem -LiteralPath $resolvedCandidateBin -File |
    Get-FileHash -Algorithm SHA256 |
    Select-Object @{Name = "Name"; Expression = { Split-Path $_.Path -Leaf }}, Hash |
    Sort-Object Name
