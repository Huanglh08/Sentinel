# Launch e2e on 3-seed smoke file (ASCII only, PS 5.1 safe)
$ErrorActionPreference = "Stop"
Set-Location "c:\Users\30970\Desktop\Folder\sentinel-main\agent-python"
$env:PYTHONIOENCODING = "utf-8"
$py = "C:\Users\30970\AppData\Local\Programs\Python\Python312\python.exe"
$p = Start-Process -FilePath $py -ArgumentList "-X","utf8","-u","-m","eval.e2e_eval","--seed","..\evaluation\seeds\e2e-smoke3.jsonl","--runs","1","--no-judge" -RedirectStandardOutput "e2e_smoke3_out.log" -RedirectStandardError "e2e_smoke3_err.log" -NoNewWindow -PassThru
Write-Output ("e2e_smoke3 PID: " + $p.Id)
