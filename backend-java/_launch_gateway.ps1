# Launch backend-java bootstrap gateway in background (ASCII only, PS 5.1 safe)
$env:JAVA_HOME = 'D:\EdgeDownload\zulu17.68.203-ca-jdk17.0.20.1-win_x64'
$java = Join-Path $env:JAVA_HOME 'bin\java.exe'
$jar = 'c:\Users\30970\Desktop\Folder\sentinel-main\backend-java\bootstrap\target\bootstrap-0.0.1-SNAPSHOT.jar'
$wd = 'c:\Users\30970\Desktop\Folder\sentinel-main\backend-java'
$p = Start-Process -FilePath $java -ArgumentList @('-jar', $jar) `
    -WorkingDirectory $wd -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $wd '_gateway_run.log') `
    -RedirectStandardError (Join-Path $wd '_gateway_run.err.log')
Write-Output ("gateway pid=" + $p.Id)
