# Launch backend-java bootstrap gateway in background (ASCII only, PS 5.1 safe)
$env:JAVA_HOME = 'D:\EdgeDownload\zulu17.68.203-ca-jdk17.0.20.1-win_x64'
$java = Join-Path $env:JAVA_HOME 'bin\java.exe'
$jar = 'c:\Users\30970\Desktop\Folder\sentinel-main\backend-java\bootstrap\target\bootstrap-0.0.1-SNAPSHOT.jar'
$wd = 'c:\Users\30970\Desktop\Folder\sentinel-main\backend-java'

# Service-to-service auth: reuse agent API_KEY as the single source of truth.
# Read from agent-python/.env (gitignored). Do NOT hardcode the real key here.
if (-not $env:SENTINEL_PYTHON_API_KEY) {
    $agentEnv = 'c:\Users\30970\Desktop\Folder\sentinel-main\agent-python\.env'
    $m = Select-String -Path $agentEnv -Pattern '^\s*API_KEY\s*=\s*(.+)$' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($m) { $env:SENTINEL_PYTHON_API_KEY = $m.Matches[0].Groups[1].Value.Trim() }
}
if (-not $env:SENTINEL_PYTHON_API_KEY) { Write-Warning 'SENTINEL_PYTHON_API_KEY unset: gateway->agent calls will 401' }

$p = Start-Process -FilePath $java -ArgumentList @('-jar', $jar) `
    -WorkingDirectory $wd -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $wd '_gateway_run.log') `
    -RedirectStandardError (Join-Path $wd '_gateway_run.err.log')
Write-Output ("gateway pid=" + $p.Id)
