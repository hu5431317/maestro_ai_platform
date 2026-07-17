# Auto-confirm OPPO install dialog
$adb = "C:\Users\Administrator\adb-platform-tools\adb.exe"

Write-Host "[1] Starting APK install..."
$job = Start-Job -ScriptBlock {
    $adb = "C:\Users\Administrator\adb-platform-tools\adb.exe"
    & $adb install -r -g "d:\pycharm\Maestro_App_Automation\maestro_ai_platform\drivers\maestro-app.apk" 2>&1
}

Write-Host "[2] Waiting for OPPO install dialog..."
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    $dump = & $adb shell "uiautomator dump /sdcard/ui.xml 2>/dev/null; cat /sdcard/ui.xml 2>/dev/null" 2>&1
    if ($dump -match "继续安装|取消安装|AppDetailActivity") {
        Write-Host "[3] Found install dialog! Tapping 'Continue Install'..."
        & $adb shell input tap 200 1408
        Start-Sleep -Seconds 2
        # Check if there's a second confirmation (open button)
        $dump2 = & $adb shell "uiautomator dump /sdcard/ui.xml 2>/dev/null; cat /sdcard/ui.xml 2>/dev/null" 2>&1
        if ($dump2 -match "完成|打开|Done|Open") {
            Write-Host "[4] Tapping 'Done'..."
            & $adb shell input tap 360 1440
        }
        break
    }
    if ($i -eq 2 -and -not ($dump -match "继续安装")) {
        Write-Host "[*] No dialog detected after 6s, installation may have auto-completed..."
        break
    }
}

Write-Host "[5] Waiting for install job..."
$result = $job | Wait-Job -Timeout 30 | Receive-Job
$job | Remove-Job -Force
Write-Host "Result: $result"

# Verify
$pkgs = & $adb shell pm list packages 2>&1 | Select-String "maestro"
Write-Host "[6] Installed maestro packages:"
$pkgs
