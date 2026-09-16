[CmdletBinding()]
param(
    [string]$InstallerPath = "",
    [string]$InstallDirectory = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $InstallerPath) {
    $packageMetadata = Get-Content -LiteralPath (Join-Path $projectRoot 'package.json') -Raw | ConvertFrom-Json
    $InstallerPath = Join-Path $projectRoot ("dist\VARIANT-1 Setup {0}.exe" -f $packageMetadata.version)
}
$resolvedInstaller = (Resolve-Path -LiteralPath $InstallerPath).Path
$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
if (-not $InstallDirectory) {
    $InstallDirectory = Join-Path $temporaryRoot (
        "variant-1-install-smoke-" + [guid]::NewGuid().ToString("N")
    )
}
$resolvedInstall = [IO.Path]::GetFullPath($InstallDirectory)
if (-not $resolvedInstall.StartsWith(
    $temporaryRoot,
    [StringComparison]::OrdinalIgnoreCase
)) {
    throw "InstallDirectory must stay under the system temporary directory"
}
if (Test-Path -LiteralPath $resolvedInstall) {
    throw "Refusing to overwrite an existing lifecycle-test directory: $resolvedInstall"
}

function Invoke-Installer {
    param([string]$Path, [string]$Destination)
    $process = Start-Process -FilePath $Path `
        -ArgumentList @("/S", "/D=$Destination") `
        -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "NSIS installer exited with code $($process.ExitCode)"
    }
}

function Invoke-PackagedSmoke {
    param([string]$Executable, [int]$Port)
    $previousExecutable = $env:VARIANT1_E2E_EXECUTABLE
    $previousPort = $env:VARIANT1_E2E_CDP_PORT
    try {
        $env:VARIANT1_E2E_EXECUTABLE = $Executable
        $env:VARIANT1_E2E_CDP_PORT = [string]$Port
        & node (Join-Path $projectRoot "scripts\test-deck-electron-smoke.js")
        if ($LASTEXITCODE -ne 0) {
            throw "installed Electron smoke exited with code $LASTEXITCODE"
        }
    }
    finally {
        $env:VARIANT1_E2E_EXECUTABLE = $previousExecutable
        $env:VARIANT1_E2E_CDP_PORT = $previousPort
    }
}

$installedExecutable = Join-Path $resolvedInstall "VARIANT-1.exe"
$uninstaller = ""
$result = [ordered]@{
    schema = "variant1.nsis-lifecycle.v1"
    installer = $resolvedInstaller
    installer_sha256 = (Get-FileHash -LiteralPath $resolvedInstaller -Algorithm SHA256).Hash.ToLowerInvariant()
    install_directory = $resolvedInstall
    first_install = $false
    first_launch = $false
    upgrade = $false
    second_launch = $false
    uninstall = $false
}

try {
    Invoke-Installer -Path $resolvedInstaller -Destination $resolvedInstall
    if (-not (Test-Path -LiteralPath $installedExecutable -PathType Leaf)) {
        throw "installed executable is missing: $installedExecutable"
    }
    $result.first_install = $true
    $firstHash = (Get-FileHash -LiteralPath $installedExecutable -Algorithm SHA256).Hash

    Invoke-PackagedSmoke -Executable $installedExecutable -Port 9341
    $result.first_launch = $true

    Invoke-Installer -Path $resolvedInstaller -Destination $resolvedInstall
    if (-not (Test-Path -LiteralPath $installedExecutable -PathType Leaf)) {
        throw "upgrade removed the installed executable"
    }
    $secondHash = (Get-FileHash -LiteralPath $installedExecutable -Algorithm SHA256).Hash
    if ($firstHash -ne $secondHash) {
        throw "same-version upgrade changed the installed executable hash"
    }
    $result.upgrade = $true

    Invoke-PackagedSmoke -Executable $installedExecutable -Port 9342
    $result.second_launch = $true

    $uninstallerItem = Get-ChildItem -LiteralPath $resolvedInstall -File |
        Where-Object { $_.Name -like "Uninstall*.exe" } |
        Select-Object -First 1
    if (-not $uninstallerItem) {
        throw "NSIS uninstaller is missing from $resolvedInstall"
    }
    $uninstaller = $uninstallerItem.FullName
    $process = Start-Process -FilePath $uninstaller -ArgumentList "/S" `
        -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "NSIS uninstaller exited with code $($process.ExitCode)"
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ((Test-Path -LiteralPath $installedExecutable) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Test-Path -LiteralPath $installedExecutable) {
        throw "uninstall left the application executable in place"
    }
    $result.uninstall = $true
    $result | ConvertTo-Json -Depth 4
}
finally {
    if ((Test-Path -LiteralPath $installedExecutable) -and $uninstaller -and (
        Test-Path -LiteralPath $uninstaller -PathType Leaf
    )) {
        Start-Process -FilePath $uninstaller -ArgumentList "/S" `
            -WindowStyle Hidden -Wait | Out-Null
    }
}
