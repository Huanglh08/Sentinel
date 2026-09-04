# Launch agent-python uvicorn server in background (ASCII only)
$py = 'C:\Users\30970\AppData\Local\Programs\Python\Python312\python.exe'
$wd = 'c:\Users\30970\Desktop\Folder\sentinel-main\agent-python'
$p = Start-Process -FilePath $py -ArgumentList @('-m', 'uvicorn', 'server:app', '--host', '0.0.0.0', '--port', '8001') `
    -WorkingDirectory $wd -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $wd '_agent_run.log') `
    -RedirectStandardError (Join-Path $wd '_agent_run.err.log')
Write-Output ("agent pid=" + $p.Id)
