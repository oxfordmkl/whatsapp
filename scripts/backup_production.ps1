# backup_production.ps1 - Phase 0 Sprint 4
# Scheduled encrypted production backup with rotation (Constitution VI.5).
#
#   dump (pg_dump -Fc) -> integrity check (pg_restore --list)
#   -> encrypt (openssl AES-256-CBC, PBKDF2, passphrase file)
#   -> delete plaintext -> rotate (keep newest 8) -> append log line
#
# Scheduled via Windows Task Scheduler (task: OxfordCRM-NightlyBackup, 02:00
# daily) - see docs/13_operations/BACKUP_RESTORE_RUNBOOK.md section 6.
# Passphrase: backups/.backup_passphrase (git-ignored). A copy MUST be stored
# off-machine (password manager) - an encrypted backup without its key is data
# loss with extra steps.
#
# Exit codes: 0 ok, 1 failure (logged to backups/backup_log.txt either way).

$ErrorActionPreference = "Stop"
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$BackupDir  = Join-Path $RepoRoot "backups"
$PassFile   = Join-Path $BackupDir ".backup_passphrase"
$LogFile    = Join-Path $BackupDir "backup_log.txt"
$Keep       = 8
$Stamp      = Get-Date -Format "yyyyMMdd_HHmm"
$PlainFile  = Join-Path $BackupDir "oxfordcrm_$Stamp.dump"
$EncFile    = "$PlainFile.enc"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    Write-Host $line
}

try {
    if (-not (Test-Path $PassFile)) { throw "Passphrase file missing: $PassFile" }
    New-Item -ItemType Directory -Force $BackupDir | Out-Null

    # 1. Resolve DATABASE_URL (never logged, never echoed)
    $vars = railway variables --json | ConvertFrom-Json
    $DbUrl = $vars.DATABASE_URL
    if (-not $DbUrl) { throw "DATABASE_URL not resolvable via railway CLI" }

    # 2. Dump
    & pg_dump --format=custom --no-owner --file=$PlainFile $DbUrl
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed (exit $LASTEXITCODE)" }
    $SizeKB = [math]::Round((Get-Item $PlainFile).Length / 1KB)

    # 3. Integrity check BEFORE encryption
    $toc = & pg_restore --list $PlainFile
    if ($LASTEXITCODE -ne 0) { throw "pg_restore --list failed on fresh dump" }
    $Tables = ($toc | Select-String "TABLE DATA").Count

    # 4. Encrypt, then remove plaintext
    & openssl enc -aes-256-cbc -pbkdf2 -salt -in $PlainFile -out $EncFile -pass "file:$PassFile"
    if ($LASTEXITCODE -ne 0) { throw "openssl encryption failed" }
    Remove-Item $PlainFile -Force -Confirm:$false

    # 5. Rotate: keep newest $Keep encrypted backups
    $old = Get-ChildItem $BackupDir -Filter "oxfordcrm_*.dump.enc" |
           Sort-Object Name -Descending | Select-Object -Skip $Keep
    foreach ($f in $old) {
        Remove-Item $f.FullName -Force -Confirm:$false
        Log "ROTATED OUT $($f.Name)"
    }

    Log "BACKUP OK $(Split-Path -Leaf $EncFile) size=${SizeKB}KB tables=$Tables kept=$((Get-ChildItem $BackupDir -Filter 'oxfordcrm_*.dump.enc').Count)"
    exit 0
}
catch {
    Log "BACKUP FAILED: $($_.Exception.Message)"
    if (Test-Path $PlainFile) { Remove-Item $PlainFile -Force -Confirm:$false }
    exit 1
}
