#!/usr/bin/env pwsh

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $Destination,

    [string] $LockFile = (Join-Path $PSScriptRoot "windows-toolchain-lock.json"),

    [string] $BaseArchive,

    [string] $PackageCache
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Assert-LockedFile {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Path,

        [Parameter(Mandatory = $true)]
        [long] $Bytes,

        [Parameter(Mandatory = $true)]
        [string] $Sha256
    )

    $file = Get-Item -LiteralPath $Path
    if ($file.Length -ne $Bytes) {
        throw "Locked file size mismatch: $Path"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        throw "Locked file SHA-256 mismatch: $Path"
    }
}

function Copy-Or-Download {
    param(
        [Parameter(Mandatory = $true)]
        [string] $Target,

        [string] $Source,

        [string] $Url
    )

    if ($Source) {
        Copy-Item -LiteralPath $Source -Destination $Target
        return
    }
    if (-not $Url) {
        throw "No source or URL was provided for $Target"
    }

    & curl.exe --fail --location --silent --show-error --retry 3 --output $Target $Url
    if ($LASTEXITCODE -ne 0) {
        throw "Download failed: $Url"
    }
}

$lockPath = (Resolve-Path -LiteralPath $LockFile).Path
$lock = Get-Content -LiteralPath $lockPath -Raw | ConvertFrom-Json
if ($lock.schema -ne 1) {
    throw "Unsupported Windows toolchain lock schema"
}
if ($lock.package_repository -ne "https://repo.msys2.org/mingw/mingw64") {
    throw "Unexpected MSYS2 package repository"
}

$destinationPath = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $destinationPath) {
    throw "Refusing to reuse Windows toolchain directory: $destinationPath"
}
New-Item -ItemType Directory -Path $destinationPath | Out-Null

$downloadDirectory = Join-Path $destinationPath "downloads"
New-Item -ItemType Directory -Path $downloadDirectory | Out-Null

$baseTarget = Join-Path $downloadDirectory $lock.msys2_base.file
$baseSource = $null
if ($BaseArchive) {
    $baseSource = (Resolve-Path -LiteralPath $BaseArchive).Path
}
Copy-Or-Download -Target $baseTarget -Source $baseSource -Url $lock.msys2_base.url
Assert-LockedFile `
    -Path $baseTarget `
    -Bytes ([long] $lock.msys2_base.bytes) `
    -Sha256 $lock.msys2_base.sha256

& $baseTarget -y "-o$destinationPath"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to extract the locked MSYS2 base self-extracting archive"
}

$msysRoot = Join-Path $destinationPath "msys64"
$bash = Join-Path $msysRoot "usr\bin\bash.exe"
if (-not (Test-Path -LiteralPath $bash -PathType Leaf)) {
    throw "The locked MSYS2 base archive has an unexpected layout"
}

$packageDirectory = Join-Path $msysRoot "var\cache\horde-toolchain"
New-Item -ItemType Directory -Path $packageDirectory | Out-Null

# The archives are authenticated by the lock before this transaction. A local,
# repository-free pacman configuration prevents rolling database state or key
# refreshes from affecting the build environment.
$pacmanConfig = Join-Path $msysRoot "etc\horde-pacman.conf"
[IO.File]::WriteAllText(
    $pacmanConfig,
    "[options]`nArchitecture = auto`nCheckSpace`nSigLevel = Never`nLocalFileSigLevel = Never`n",
    [Text.Encoding]::ASCII
)

$packageNames = [Collections.Generic.HashSet[string]]::new(
    [StringComparer]::Ordinal
)
foreach ($package in $lock.packages) {
    if (-not $packageNames.Add([string] $package.name)) {
        throw "Duplicate package in Windows toolchain lock: $($package.name)"
    }
    $target = Join-Path $packageDirectory $package.file
    $source = $null
    if ($PackageCache) {
        $source = Join-Path ([IO.Path]::GetFullPath($PackageCache)) $package.file
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Locked package is absent from the local cache: $source"
        }
    }
    $url = "$($lock.package_repository)/$($package.file)"
    Copy-Or-Download -Target $target -Source $source -Url $url
    Assert-LockedFile `
        -Path $target `
        -Bytes ([long] $package.bytes) `
        -Sha256 $package.sha256
}

$previousMsystem = $env:MSYSTEM
$previousChere = $env:CHERE_INVOKING
try {
    $env:MSYSTEM = "MINGW64"
    $env:CHERE_INVOKING = "1"
    & $bash --noprofile --norc -c "export PATH=/usr/bin; pacman --config /etc/horde-pacman.conf -U --noconfirm /var/cache/horde-toolchain/*.pkg.tar.zst"
    if ($LASTEXITCODE -ne 0) {
        throw "Locked MSYS2 package transaction failed"
    }

    $installedLines = @(& $bash --noprofile --norc -c "export PATH=/usr/bin; pacman --config /etc/horde-pacman.conf -Q")
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inventory the locked MSYS2 installation"
    }
}
finally {
    $env:MSYSTEM = $previousMsystem
    $env:CHERE_INVOKING = $previousChere
}

$installed = @{}
foreach ($line in $installedLines) {
    $parts = $line -split " ", 2
    if ($parts.Count -eq 2) {
        $installed[$parts[0]] = $parts[1]
    }
}
foreach ($package in $lock.packages) {
    if ($installed[$package.name] -ne $package.version) {
        throw "Installed package does not match the lock: $($package.name)"
    }
}

$unexpected = @(
    $installed.Keys |
        Where-Object {
            $_ -like "mingw-w64-x86_64-*" -and -not $packageNames.Contains($_)
        } |
        Sort-Object
)
if ($unexpected.Count -ne 0) {
    throw "Unexpected MINGW64 packages were installed: $($unexpected -join ', ')"
}

$lockSha256 = (Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "MSYS2_ROOT=$msysRoot"
Write-Output "WINDOWS_TOOLCHAIN_LOCK_SHA256=$lockSha256"
