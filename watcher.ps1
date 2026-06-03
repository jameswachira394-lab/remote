# ============================================================
# watcher.ps1
# Auto-deploy: pulls from git, restarts bot only on changes
# Place this file in C:\TradingBot\ob_mt5_system\
# Run once: .\watcher.ps1
# ============================================================

$repoPath   = "C:\TradingBot\ob_mt5_system"
$mainScript = "main.py"
$branch     = "main"
$logFile    = "C:\TradingBot\ob_mt5_system\logs\watcher.log"
$pollSecs   = 30   # how often to check for changes (seconds)

# ── helpers ─────────────────────────────────────────────────
function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "$ts | WATCHER | $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line
}

function Stop-Bot {
    $procs = Get-Process python -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -like "*main.py*" }
    if ($procs) {
        $procs | Stop-Process -Force
        Write-Log "Bot process stopped (PID $($procs.Id -join ', '))"
        Start-Sleep -Seconds 2
    } else {
        Write-Log "No running bot process found"
    }
}

function Start-Bot {
    $proc = Start-Process python `
        -ArgumentList $mainScript `
        -WorkingDirectory $repoPath `
        -PassThru `
        -WindowStyle Hidden
    Write-Log "Bot started (PID $($proc.Id))"
}

function Get-LocalHash {
    return (git -C $repoPath rev-parse HEAD 2>$null).Trim()
}

function Get-RemoteHash {
    git -C $repoPath fetch origin $branch --quiet 2>$null
    return (git -C $repoPath rev-parse "origin/$branch" 2>$null).Trim()
}

# ── startup ──────────────────────────────────────────────────
Write-Log "========================================"
Write-Log "Watcher started | repo=$repoPath | branch=$branch | poll=${pollSecs}s"
Write-Log "========================================"

# Ensure we are on the right branch
git -C $repoPath checkout $branch --quiet 2>$null

# Start the bot immediately on watcher launch
Write-Log "Initial start..."
Start-Bot
$lastHash = Get-LocalHash
Write-Log "Current hash: $lastHash"

# ── main loop ────────────────────────────────────────────────
while ($true) {
    Start-Sleep -Seconds $pollSecs

    $remoteHash = Get-RemoteHash

    if ([string]::IsNullOrEmpty($remoteHash)) {
        Write-Log "WARNING: could not reach remote — git fetch failed. Skipping."
        continue
    }

    if ($remoteHash -ne $lastHash) {
        Write-Log "Change detected! local=$lastHash  remote=$remoteHash"

        # Pull changes
        git -C $repoPath reset --hard "origin/$branch" --quiet
        $pulled = git -C $repoPath log --oneline -5
        Write-Log "Pulled. Recent commits:"
        $pulled | ForEach-Object { Write-Log "  $_" }

        # Reinstall dependencies if requirements.txt changed
        $changedFiles = git -C $repoPath diff --name-only "$lastHash" HEAD
        if ($changedFiles -contains "requirements.txt") {
            Write-Log "requirements.txt changed — running pip install..."
            pip install -r "$repoPath\requirements.txt" --quiet
            Write-Log "pip install done"
        }

        # Restart bot
        Stop-Bot
        Start-Bot

        $lastHash = $remoteHash
        Write-Log "Deploy complete. Watching for next change..."
    } else {
        Write-Log "No change (hash=$lastHash)"
    }
}