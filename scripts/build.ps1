param([string]$ToolchainRoot, [switch]$NoOp)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($NoOp) { Write-Output 'noop: build the native frontend using an explicit local SDK'; exit 0 }
if (-not $ToolchainRoot) { throw 'Pass -ToolchainRoot pointing to cmake-clang-v1 1.0.14 for Windows.' }
$root = Split-Path $PSScriptRoot -Parent
$sdk = (Resolve-Path -LiteralPath $ToolchainRoot).Path
$metadata = Get-Content -LiteralPath (Join-Path $sdk 'retcomm-toolchain.json') -Raw | ConvertFrom-Json
if ($metadata.id -ne 'cmake-clang-v1' -or $metadata.version -ne '1.0.14' -or $metadata.os -ne 'windows-x64') {
    throw 'Expected cmake-clang-v1 1.0.14 Windows x64 SDK.'
}
$ui = Get-Content -LiteralPath (Join-Path $root 'src/versions/UI.json') -Raw | ConvertFrom-Json
$archive = Join-Path $root ('src/versions/' + $ui.source_package.file)
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $ui.source_package.sha256) {
    throw 'Included launcher source checksum differs from its version record.'
}
$dependencies = Join-Path $root 'build/dependencies/ui'
New-Item -ItemType Directory -Path $dependencies -Force | Out-Null
Expand-Archive -LiteralPath $archive -DestinationPath $dependencies -Force
$build = Join-Path $root 'build/native'
$priorPath = $env:PATH
try {
    $env:PATH = (Join-Path $sdk 'bin') + [IO.Path]::PathSeparator + $priorPath
    & (Join-Path $sdk 'bin/cmake.exe') -S (Join-Path $root 'native') -B $build -G Ninja `
        "-DCMAKE_MAKE_PROGRAM=$sdk/bin/ninja.exe" "-DCMAKE_CXX_COMPILER=$sdk/bin/clang++.exe" `
        "-DSDL3_DIR=$root/src/versions/sdl-install/lib/cmake/SDL3" `
        "-DIMGUI_DIR=$dependencies/src/third_party/imgui" "-DSTB_DIR=$dependencies/src/third_party" `
        -DCMAKE_BUILD_TYPE=Release
    if ($LASTEXITCODE) { throw 'Native CMake configuration failed.' }
    & (Join-Path $sdk 'bin/cmake.exe') --build $build -j 1
    if ($LASTEXITCODE) { throw 'Native frontend build failed.' }
} finally {
    $env:PATH = $priorPath
}
Write-Output "Native frontend: $build\Workbench Studio.exe"
