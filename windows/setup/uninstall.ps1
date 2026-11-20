# Shwe Muse — remove the offline server (started by "Uninstall Shwe Muse.bat").
# Stops the server and removes its scheduled tasks, firewall rule, program files
# and shortcuts. The DATA (C:\ShweMuse\data, backups, config.env) is KEPT, so a
# later reinstall continues where it left off. Delete C:\ShweMuse yourself to wipe it.

Add-Type -AssemblyName System.Windows.Forms
$Root = 'C:\ShweMuse'

$answer = [System.Windows.Forms.MessageBox]::Show(
    "Shwe Muse server ကို ဖြုတ်မှာ သေချာလား?`r`n`r`nData နဲ့ backup တွေ (C:\ShweMuse\data, backups) ကိုတော့ မဖျက်ပါ။",
    'Shwe Muse', 'YesNo', 'Question')
if ($answer -ne 'Yes') { exit }

foreach ($t in 'ShweMuse Server', 'ShweMuse Backup', 'ShweMuse Cloud Sync', 'ShweMuse Update') {
    Stop-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue
}
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$Root\python\*" } |
    ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }
Start-Sleep -Seconds 2
Remove-NetFirewallRule -DisplayName 'Shwe Muse Server' -ErrorAction SilentlyContinue

$desktop = [Environment]::GetFolderPath('CommonDesktopDirectory')
Remove-Item "$desktop\Shwe Muse.lnk", "$desktop\Shwe Muse TV.lnk", "$desktop\Shwe Muse.url" -ErrorAction SilentlyContinue
Remove-Item "$Root\python", "$Root\app", "$Root\app.prev", "$Root\app.new", "$Root\app.failed", "$Root\updates", "$Root\run_update.pyc", "$Root\For-Other-Computers" -Recurse -Force -ErrorAction SilentlyContinue

[System.Windows.Forms.MessageBox]::Show("Shwe Muse ကို ဖြုတ်ပြီးပါပြီ။`r`nData: $Root\data", 'Shwe Muse', 'OK', 'Information') | Out-Null
