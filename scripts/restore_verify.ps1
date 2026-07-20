# restore_verify.ps1 - Phase 0 Sprint 4
# Automated restore verification for encrypted backups (Constitution I.5).
#
#   decrypt -> pg_restore --list -> restore into scratch DB on the Railway
#   instance -> row-count spot check -> DROP scratch DB
#
# Usage:  powershell -File scripts\restore_verify.ps1 [path\to\backup.dump.enc]
#         (defaults to the newest encrypted backup)
# Exit codes: 0 = restore verified, 1 = failure. Log: backups/backup_log.txt.
# Production database is NEVER touched - scratch DB only.

param([string]$EncFile = "")

$ErrorActionPreference = "Stop"
$RepoRoot  = Split-Path -Parent $PSScriptRoot
$BackupDir = Join-Path $RepoRoot "backups"
$PassFile  = Join-Path $BackupDir ".backup_passphrase"
$LogFile   = Join-Path $BackupDir "backup_log.txt"
$Scratch   = "oxford_restore_verify"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Add-Content -Path $LogFile -Value $line -Encoding utf8
    Write-Host $line
}

$Plain = Join-Path $BackupDir "_restore_verify_tmp.dump"

try {
    if (-not $EncFile) {
        $latest = Get-ChildItem $BackupDir -Filter "oxfordcrm_*.dump.enc" |
                  Sort-Object Name -Descending | Select-Object -First 1
        if (-not $latest) { throw "No encrypted backups found in $BackupDir" }
        $EncFile = $latest.FullName
    }

    # 1. Decrypt
    & openssl enc -d -aes-256-cbc -pbkdf2 -in $EncFile -out $Plain -pass "file:$PassFile"
    if ($LASTEXITCODE -ne 0) { throw "decryption failed (wrong passphrase or corrupt file)" }

    # 2. Structural check
    $toc = & pg_restore --list $Plain
    if ($LASTEXITCODE -ne 0) { throw "pg_restore --list failed" }
    $Tables = ($toc | Select-String "TABLE DATA").Count

    # 3. Scratch restore on the Railway instance (production DB untouched)
    $vars = railway variables --json | ConvertFrom-Json
    $DbUrl = $vars.DATABASE_URL
    if (-not $DbUrl) { throw "DATABASE_URL not resolvable" }
    $idx = $DbUrl.LastIndexOf("/")
    $ScratchUrl = $DbUrl.Substring(0, $idx) + "/$Scratch"

    & psql $DbUrl -q -c "DROP DATABASE IF EXISTS $Scratch;" -c "CREATE DATABASE $Scratch;"
    if ($LASTEXITCODE -ne 0) { throw "scratch DB create failed" }
    & pg_restore --no-owner --no-privileges --dbname=$ScratchUrl $Plain
    if ($LASTEXITCODE -ne 0) { throw "pg_restore into scratch failed" }

    # 4. Spot check
    $t = (& psql $ScratchUrl -t -A -c "SELECT count(*) FROM tenants;").Trim()
    $c = (& psql $ScratchUrl -t -A -c "SELECT count(*) FROM conversation_state;").Trim()
    if ([int]$t -lt 1) { throw "restored DB has no tenants - restore invalid" }

    # 5. Cleanup
    & psql $DbUrl -q -c "DROP DATABASE $Scratch;"

    Log "RESTORE VERIFY OK $(Split-Path -Leaf $EncFile) tables=$Tables tenants=$t conversation_state=$c"
    exit 0
}
catch {
    Log "RESTORE VERIFY FAILED: $($_.Exception.Message)"
    try {
        if ($DbUrl) { & psql $DbUrl -q -c "DROP DATABASE IF EXISTS $Scratch;" }
    } catch {}
    exit 1
}
finally {
    if (Test-Path $Plain) { Remove-Item $Plain -Force -Confirm:$false }
}
