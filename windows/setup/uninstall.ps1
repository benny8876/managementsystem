# Hospital — remove the offline server (started by "Uninstall Hospital.bat").
# Stops the server and removes its scheduled tasks, firewall rule, program files
# and shortcuts. The DATA (C:\Hospital\data, backups, config.env) is KEPT, so a
# later reinstall continues where it left off. Delete C:\Hospital yourself to wipe it.

Add-Type -AssemblyName System.Windows.Forms
$Root = 'C:\Hospital'

$answer = [System.Windows.Forms.MessageBox]::Show(
    "Hospital server ကို ဖြုတ်မှာ သေချာလား?`r`n`r`nData နဲ့ backup တွေ (C:\Hospital\data, backups) ကိုတော့ မဖျက်ပါ။",
    'Hospital', 'YesNo', 'Question')
if ($answer -ne 'Yes') { exit }

foreach ($t in 'Hospital Server', 'Hospital Backup', 'Hospital Cloud Sync', 'Hospital Update') {
    Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$Root\python\*" } |
    ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }
Start-Sleep -Seconds 2
Remove-NetFirewallRule -DisplayName 'Hospital Server' -ErrorAction SilentlyContinue

$desktop = [Environment]::GetFolderPath('CommonDesktopDirectory')
Remove-Item "$desktop\Hospital.lnk", "$desktop\Hospital TV.lnk", "$desktop\Hospital.url" -ErrorAction SilentlyContinue
Remove-Item "$Root\python", "$Root\app", "$Root\app.prev", "$Root\app.new", "$Root\app.failed", "$Root\updates", "$Root\run_update.pyc", "$Root\For-Other-Computers" -Recurse -Force -ErrorAction SilentlyContinue

[System.Windows.Forms.MessageBox]::Show("Hospital ကို ဖြုတ်ပြီးပါပြီ။`r`nData: $Root\data", 'Hospital', 'OK', 'Information') | Out-Null
