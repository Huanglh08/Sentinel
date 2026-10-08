# Full e2e baseline: 102 seeds x 3 runs with judge (ASCII only, PS 5.1 safe)
# Usage: .\_launch_e2e_baseline3.ps1
# Then:  update $pidTarget in _progress_bar.ps1, run it in a separate window.
$ErrorActionPreference = "Stop"
Set-Location "c:\Users\30970\Desktop\Folder\sentinel-main\agent-python"
$env:PYTHONIOENCODING = "utf-8"
$py = "C:\Users\30970\AppData\Local\Programs\Python\Python312\python.exe"
$ts = Get-Date -Format "yyyyMMdd-HHmmss"

Write-Output "=== launching e2e-baseline3 (102 seeds x 3 runs) at $ts ==="
$p = Start-Process -FilePath $py -ArgumentList @(
    '-X','utf8','-u','-m','eval.e2e_eval',
    '--seed','..\evaluation\seeds\e2e-seeds.jsonl',
    '--runs','3',
    '--name',"e2e-baseline3-$ts"
) -WorkingDirectory "c:\Users\30970\Desktop\Folder\sentinel-main\agent-python" `
  -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput "e2e_baseline3_out.log" `
  -RedirectStandardError "e2e_baseline3_err.log"

Write-Output ("e2e pid=" + $p.Id)
Write-Output "logs: e2e_baseline3_out.log / e2e_baseline3_err.log"
