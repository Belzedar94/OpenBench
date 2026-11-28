# Fresh load of each store, then the kill -9 test.
# Usage: run_crash.ps1 -Exe <bench.exe> -Data <dataset dir> -Work <scratch dir> -Out <crash.jsonl>
#        [-Stores mmap,lmdb,...] [-Trials 30] [-PgBin <dir>] [-PgData <data dir>] [-PgTrials 8]
param(
    [Parameter(Mandatory)] [string] $Exe,
    [Parameter(Mandatory)] [string] $Data,
    [Parameter(Mandatory)] [string] $Work,
    [Parameter(Mandatory)] [string] $Out,
    [string[]] $Stores = @('mmap', 'memwal', 'lmdb', 'sqlite', 'rocksdb', 'redb', 'fjall', 'pgint', 'pgv1'),
    [int] $Trials = 30,
    [int] $PgTrials = 8,
    [string] $PgBin = "",
    [string] $PgData = "",
    [int] $PgPort = 54329
)
$ErrorActionPreference = 'Continue'
$name = Split-Path $Data -Leaf
New-Item -ItemType Directory -Force $Work | Out-Null
# Small log before each checkpoint, so that kills land in checkpoints too.
$env:MMAP_WAL_CKPT_MB = "2"
foreach ($k in $Stores) {
    $db = Join-Path $Work "${name}_crash_$k"
    $pgdb = "bench_${name}_crash_$k"
    $pg = "host=127.0.0.1 port=$PgPort user=postgres dbname=$pgdb"
    $extra = @()
    $n = $Trials
    if ($k -like 'pg*') {
        & "$PgBin\psql.exe" -h 127.0.0.1 -p $PgPort -U postgres -d postgres -X -q -c "drop database if exists $pgdb" -c "create database $pgdb" 2>&1 | Out-Null
        $extra = @('--pgctl', "$PgBin\pg_ctl.exe", '--pgdata', $PgData)
        $n = $PgTrials
    }
    Write-Host ("[{0}] {1}: load" -f (Get-Date -Format HH:mm:ss), $k)
    & $Exe load --store $k --data $Data --db $db --pg $pg 2>&1 | Out-Null
    Write-Host ("[{0}] {1}: {2} kills" -f (Get-Date -Format HH:mm:ss), $k, $n)
    $o = & $Exe crash --store $k --data $Data --db $db --pg $pg --trials $n --out $Out @extra 2>&1
    $o | Select-String 'crash_summary' | ForEach-Object { Write-Host "  $_" }
    if ($LASTEXITCODE -ne 0) { Write-Host "  crash test FAILED ($LASTEXITCODE)"; $o | Select-Object -Last 4 | ForEach-Object { Write-Host "  $_" } }
    if ($k -like 'pg*') { & "$PgBin\psql.exe" -h 127.0.0.1 -p $PgPort -U postgres -d postgres -X -q -c "drop database if exists $pgdb" 2>&1 | Out-Null }
    elseif (Test-Path $db) { Remove-Item -Recurse -Force $db }
}
Write-Host ("[{0}] done" -f (Get-Date -Format HH:mm:ss))
