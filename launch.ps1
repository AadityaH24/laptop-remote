# Starts Laptop Remote in THIS console window. It does not open a browser.
#
# The window is the server: the banner, the PIN and every log line appear here,
# and closing the window (or Ctrl+C) stops the server. Safe to click twice: if it
# is already running this window follows its log instead of starting a second one.
$ErrorActionPreference = 'SilentlyContinue'

$root  = Split-Path -Parent $MyInvocation.MyCommand.Path
$port  = 8765
$path  = '/laptop-remote/'
$tsExe = 'C:\Program Files\Tailscale\tailscale.exe'
$log   = Join-Path $root 'server.log'

# Never depend on the caller's working directory.
Set-Location $root

try { $Host.UI.RawUI.WindowTitle = 'Laptop Remote - server' } catch {}
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

function Test-App {
  # Ask the app itself. A port listener is not proof: `tailscale serve --tcp`
  # also listens on this port, and it is still there when python has died -
  # which used to make this launcher report "already running" and start nothing.
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port${path}api/status" `
                           -UseBasicParsing -TimeoutSec 3
    if ($r.StatusCode -ne 200) { return $false }
    return [bool](($r.Content | ConvertFrom-Json).PSObject.Properties['host'])
  } catch { return $false }
}

function Ensure-Serve {
  # Make sure tailscale forwards this port into the tailnet. Idempotent, and
  # silent when an https or tcp mapping already points at the local server.
  $status = (& $tsExe serve status 2>$null) -join "`n"
  if ($status -match "proxy\s+(https?://)?(127\.0\.0\.1|localhost):$port\b") { return }
  if ($status -match "tcp://[^\s]*:$port\b") { return }
  & $tsExe serve --bg --tcp $port $port 2>$null | Out-Null
}

function Get-AppUrl {
  $ip = Get-TsIP
  if ($ip) { return "http://${ip}:$port$path" }
  return "http://127.0.0.1:$port$path"
}

function Get-TsIP {
  if (Test-Path $tsExe) {
    $ip = (& $tsExe ip -4 2>$null | Select-Object -First 1)
    if ("$ip".Trim() -match '^\d+\.\d+\.\d+\.\d+$') { return "$ip".Trim() }
  }
  $a = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -like '100.*' -and $_.InterfaceAlias -like '*Tailscale*' } |
       Select-Object -First 1
  if ($a) { return $a.IPAddress }
  return $null
}

function Get-TsState {
  if (-not (Test-Path $tsExe)) { return 'not installed' }
  $json = (& $tsExe status --json 2>$null) -join ''
  if (-not $json) { return 'not running' }
  try {
    $s = $json | ConvertFrom-Json
    if ($s.BackendState) { return $s.BackendState }   # Running / Stopped / NeedsLogin
  } catch {}
  return 'unknown'
}

function Stop-With($msg) {
  Write-Host ""
  Write-Host "  $msg" -ForegroundColor Red
  Write-Host ""
  Read-Host "  Press Enter to close"
  exit 1
}

# ---------------------------------------------------------------------------
# already running? follow its log instead of starting a second copy
# ---------------------------------------------------------------------------
$running = Test-App
if ($running) {
  Ensure-Serve
  $url = Get-AppUrl
  Write-Host ""
  Write-Host "  Laptop Remote is already running." -ForegroundColor Green
  Write-Host "  $url"
  Write-Host "  Open that URL yourself when you want it (no browser is started)." -ForegroundColor DarkGray

  if (Test-Path $log) {
    Write-Host "  Following $log" -ForegroundColor DarkGray
    Write-Host "  Ctrl+C stops following. The server keeps running." -ForegroundColor DarkGray
    Write-Host ("  " + ('-' * 58)) -ForegroundColor DarkGray
    Get-Content $log -Wait -Tail 15
  } else {
    Write-Host "  No log file yet, so there is nothing to follow." -ForegroundColor DarkGray
    Read-Host "  Press Enter to close"
  }
  exit 0
}

# ---------------------------------------------------------------------------
# cold start: wait for the Tailscale address, then run the server here
# ---------------------------------------------------------------------------
$tsip = Get-TsIP
if (-not $tsip) {
  Write-Host ""
  Write-Host "  Waiting for Tailscale..." -ForegroundColor DarkGray
  $svc = Get-Service Tailscale -ErrorAction SilentlyContinue
  if ($svc -and $svc.Status -ne 'Running') {
    # Normally fails without elevation, but costs nothing to try.
    Start-Service Tailscale -ErrorAction SilentlyContinue
  }
  for ($i = 0; $i -lt 120; $i++) {          # up to 60s
    Start-Sleep -Milliseconds 500
    $tsip = Get-TsIP
    if ($tsip) { break }
  }
}

if (-not $tsip) {
  $hint = switch (Get-TsState) {
    'NeedsLogin' { "Tailscale is signed out. Open Tailscale, sign in, then click this again." }
    'Stopped'    { "Tailscale is stopped. Start it from the tray icon, then click this again." }
    default      { "Tailscale has no 100.x address yet. Start Tailscale (or reboot), then click this again." }
  }
  Stop-With "Laptop Remote only listens on the tailnet, so it needs Tailscale connected.`n`n  $hint"
}

$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
  # First run: no venv yet, so let the visible setup script do its thing.
  Start-Process -FilePath (Join-Path $root 'run.bat') -WorkingDirectory $root
  exit 0
}

# Tailscale forwarding is what turns 127.0.0.1 into a URL your devices can
# reach. Set it up before the server starts so the banner reports the real URL.
Ensure-Serve
$url = Get-AppUrl

Write-Host ""
Write-Host "  URL  $url" -ForegroundColor Green
Write-Host "  Starting Laptop Remote - logs below. Ctrl+C or closing this window stops it." -ForegroundColor DarkGray
Write-Host ("  " + ('-' * 58)) -ForegroundColor DarkGray

& $py -u (Join-Path $root 'server.py')
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0 -or $code -eq 130) {
  Write-Host "  Server stopped." -ForegroundColor Yellow
} else {
  Write-Host "  Server exited with code $code. Last log lines:" -ForegroundColor Red
  Get-Content $log -Tail 12 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "    $_" }
  # Only hold the window open when there is something to read; a clean stop
  # should not leave a windowless process waiting for Enter.
  Write-Host ""
  Read-Host "  Press Enter to close"
}
