# Load every store from one dataset and run the suite on it.
# Usage: run_scale.ps1 -Exe <bench.exe> -Data <dataset dir> -Work <scratch dir> -Out <results.jsonl> -Tag <label>
#        [-Stores mem,mmap,lmdb,...] [-PgBin <dir with psql.exe>] [-Keep] [suite counts...]
param(
    [Parameter(Mandatory)] [string] $Exe,
    [Parameter(Mandatory)] [string] $Data,
    [Parameter(Mandatory)] [string] $Work,
    [Parameter(Mandatory)] [string] $Out,
    [string] $Tag = "",
    [string[]] $Stores = @('mem', 'memwal', 'mmap', 'lmdb', 'sqlite', 'rocksdb', 'redb', 'fjall', 'pgint', 'pgv1'),
    [string] $PgBin = "",
    [int] $PgPort = 54329,
    [switch] $Keep,
    [switch] $NoDigest,
    [int] $Descents = 2000, [double] $ReadSeconds = 5, [int] $Readers = 8,
    [int] $Ingest1 = 500, [int] $Ingest32 = 200, [int] $Climb32 = 200, [int] $MixedBatches = 100,
    [int] $CacheMb = 2048
)
$ErrorActionPreference = 'Continue'
$name = Split-Path $Data -Leaf
New-Item -ItemType Directory -Force $Work | Out-Null
foreach ($k in $Stores) {
    $db = Join-Path $Work "${name}_$k"
    $pgdb = "bench_${name}_$k"
    $pg = "host=127.0.0.1 port=$PgPort user=postgres dbname=$pgdb"
    if ($k -like 'pg*') {
        & "$PgBin\psql.exe" -h 127.0.0.1 -p $PgPort -U postgres -d postgres -X -q -c "drop database if exists $pgdb" -c "create database $pgdb" 2>&1 | Out-Null
    }
    Write-Host ("[{0}] {1}: load" -f (Get-Date -Format HH:mm:ss), $k)
    if ($k -ne 'mem') {
        & $Exe load --store $k --data $Data --db $db --pg $pg --out $Out --tag $Tag --cache-mb $CacheMb 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Host "  load FAILED ($LASTEXITCODE)"; continue }
    }
    Write-Host ("[{0}] {1}: suite" -f (Get-Date -Format HH:mm:ss), $k)
    $dig = @(); if ($NoDigest) { $dig = @('--no-digest') }
    $o = & $Exe suite --store $k --data $Data --db $db --pg $pg --out $Out --tag $Tag --cache-mb $CacheMb @dig `
        --descents $Descents --read-seconds $ReadSeconds --readers $Readers `
        --ingest1 $Ingest1 --ingest32 $Ingest32 --climb32 $Climb32 --mixed-batches $MixedBatches 2>&1
    if ($LASTEXITCODE -ne 0) { Write-Host "  suite FAILED ($LASTEXITCODE)"; $o | Select-Object -Last 5 | ForEach-Object { Write-Host "  $_" } }
    if (-not $Keep) {
        if ($k -like 'pg*') { & "$PgBin\psql.exe" -h 127.0.0.1 -p $PgPort -U postgres -d postgres -X -q -c "drop database if exists $pgdb" 2>&1 | Out-Null }
        elseif (Test-Path $db) { Remove-Item -Recurse -Force $db }
    }
}
Write-Host ("[{0}] done" -f (Get-Date -Format HH:mm:ss))
