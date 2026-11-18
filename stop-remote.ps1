# Stops the Laptop Remote server (whichever way it was started).
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*server.py*' } |
  ForEach-Object {
    Write-Host "Stopping PID $($_.ProcessId)..."
    Stop-Process -Id $_.ProcessId -Force
  }
Remove-Item (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'server.pid') -ErrorAction SilentlyContinue
Write-Host 'Laptop Remote stopped.'
