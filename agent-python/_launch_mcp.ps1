# Launch MCP server (SSE transport) in background on port 8002 (8000 is milvus-attu)
# ASCII only - PS 5.1 GBK safe
$py = 'C:\Users\30970\AppData\Local\Programs\Python\Python312\python.exe'
$wd = 'c:\Users\30970\Desktop\Folder\sentinel-main\agent-python'
$p = Start-Process -FilePath $py -ArgumentList @('mcp-server-py\server.py', '--transport', 'sse', '--port', '8002') `
    -WorkingDirectory $wd -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $wd '_mcp_run.log') `
    -RedirectStandardError (Join-Path $wd '_mcp_run.err.log')
Write-Output ("mcp pid=" + $p.Id)
