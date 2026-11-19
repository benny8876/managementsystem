# Shwe Muse — offline site server installer (Windows 10/11), for the hospital or the clinic
# (setup\site.env says which; the two ZIPs differ only in that file and initial.db).
# Started by "Install Shwe Muse.bat" (already elevated). Shows one window with an
# Install button, then: copies the app to C:\ShweMuse, starts it at every boot,
# opens the firewall port, schedules daily backups and makes desktop shortcuts.
# Running it again updates the app and keeps all data (C:\ShweMuse\data).

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$Root      = 'C:\ShweMuse'
$Payload   = Join-Path (Split-Path $PSScriptRoot -Parent) 'payload'
$TaskName  = 'ShweMuse Server'
$BackupTask = 'ShweMuse Backup'
$SyncTask  = 'ShweMuse Cloud Sync'
$RuleName  = 'Shwe Muse Server'
$Green     = [System.Drawing.Color]::FromArgb(20, 141, 0)
$MmFont    = 'Myanmar Text'

$Site = 'clinic'
$siteEnv = Join-Path $PSScriptRoot 'site.env'
if (Test-Path $siteEnv) {
    foreach ($line in Get-Content $siteEnv) {
        if ($line -match '^\s*SITE\s*=\s*(\w+)') { $Site = $Matches[1].ToLower() }
    }
}
$SiteName = if ($Site -eq 'hospital') { 'Hospital' } else { 'Clinic' }
$SiteMm   = if ($Site -eq 'hospital') { 'ဆေးရုံ' } else { 'ဆေးခန်း' }

function Write-Log([string]$msg) {
    New-Item -ItemType Directory -Force -Path "$Root\logs" | Out-Null
    Add-Content -Path "$Root\logs\install.log" -Value ("{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg) -Encoding UTF8
}

function Get-LanIPs {
    Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
        Sort-Object InterfaceMetric | Select-Object -ExpandProperty IPAddress
}

function Get-Url([string]$hostName, [int]$port) {
    if ($port -eq 80) { "http://$hostName" } else { "http://${hostName}:$port" }
}

function Get-EdgePath {
    foreach ($p in @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function New-Shortcut([string]$path, [string]$target, [string]$arguments, [string]$icon) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $arguments
    if ($icon -and (Test-Path $icon)) { $lnk.IconLocation = $icon }
    $lnk.Save()
}

function Stop-ShweMuse {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath -like "$Root\python\*" } |
        ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }
    Start-Sleep -Seconds 2
}

function Copy-Folder([string]$from, [string]$to) {
    robocopy $from $to /MIR /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copy failed: $from -> $to (robocopy $LASTEXITCODE)" }
}

function Read-Config {
    $cfg = @{}
    if (Test-Path "$Root\config.env") {
        foreach ($line in Get-Content "$Root\config.env") {
            if ($line -match '^\s*([A-Z_]+)\s*=\s*(.*)$') { $cfg[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $cfg
}

# ---------------------------------------------------------------- the window
$form = New-Object System.Windows.Forms.Form
$form.Text = "Shwe Muse — $SiteName Server Setup"
$form.Size = New-Object System.Drawing.Size(620, 520)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.BackColor = [System.Drawing.Color]::White
$ico = Join-Path $PSScriptRoot 'shwemuse.ico'
if (Test-Path $ico) { $form.Icon = New-Object System.Drawing.Icon($ico) }

$logo = New-Object System.Windows.Forms.PictureBox
$logo.Location = New-Object System.Drawing.Point(30, 25)
$logo.Size = New-Object System.Drawing.Size(80, 80)
$logo.SizeMode = 'Zoom'
$logoPng = Join-Path $PSScriptRoot 'logo.png'
if (Test-Path $logoPng) { $logo.Image = [System.Drawing.Image]::FromFile($logoPng) }
$form.Controls.Add($logo)

$title = New-Object System.Windows.Forms.Label
$title.Text = "Shwe Muse $SiteName Server"
$title.Font = New-Object System.Drawing.Font('Segoe UI', 18, [System.Drawing.FontStyle]::Bold)
$title.ForeColor = $Green
$title.Location = New-Object System.Drawing.Point(125, 30)
$title.AutoSize = $true
$form.Controls.Add($title)

$sub = New-Object System.Windows.Forms.Label
$sub.Text = 'Internet မလိုဘဲ LAN ကနေ သုံးနိုင်တဲ့ offline server'
$sub.Font = New-Object System.Drawing.Font($MmFont, 11)
$sub.ForeColor = [System.Drawing.Color]::DimGray
$sub.Location = New-Object System.Drawing.Point(128, 72)
$sub.AutoSize = $true
$form.Controls.Add($sub)

$info = New-Object System.Windows.Forms.Label
$info.Text = "ဒီ computer ကို $SiteMm ရဲ့ main server အဖြစ် ထည့်ပါမယ်။`r`n`r`n" +
             "•  C:\ShweMuse ထဲ ထည့်ပြီး computer ဖွင့်တိုင်း အလိုလို စပါမယ်`r`n" +
             "•  တခြား computer တွေက Chrome / Edge နဲ့ ဒီ computer ရဲ့ IP ကို ဖွင့်ရုံပါပဲ`r`n" +
             "•  နေ့တိုင်း database ကို အလိုလို backup လုပ်ပါမယ်`r`n" +
             "•  Internet ရတာနဲ့ data ကို cloud ဆီ ပို့ပြီး item / ဈေးနှုန်း ကို cloud ကနေ ယူပါမယ်`r`n" +
             "•  Token QR က ဖုန်း data နဲ့ ဘယ်နေရာကမဆို ဖွင့်လို့ရပါတယ်`r`n" +
             "•  ပြန် install လုပ်ရင် (update) data တွေ မပျက်ပါ"
$info.Font = New-Object System.Drawing.Font($MmFont, 11)
$info.Location = New-Object System.Drawing.Point(32, 125)
$info.Size = New-Object System.Drawing.Size(560, 190)
$form.Controls.Add($info)

$progress = New-Object System.Windows.Forms.ProgressBar
$progress.Location = New-Object System.Drawing.Point(32, 330)
$progress.Size = New-Object System.Drawing.Size(540, 22)
$progress.Maximum = 9
$progress.Visible = $false
$form.Controls.Add($progress)

$status = New-Object System.Windows.Forms.Label
$status.Font = New-Object System.Drawing.Font($MmFont, 10)
$status.Location = New-Object System.Drawing.Point(32, 360)
$status.Size = New-Object System.Drawing.Size(540, 30)
$form.Controls.Add($status)

$button = New-Object System.Windows.Forms.Button
$button.Text = 'Install'
$button.Font = New-Object System.Drawing.Font('Segoe UI', 14, [System.Drawing.FontStyle]::Bold)
$button.BackColor = $Green
$button.ForeColor = [System.Drawing.Color]::White
$button.FlatStyle = 'Flat'
$button.FlatAppearance.BorderSize = 0
$button.Location = New-Object System.Drawing.Point(190, 400)
$button.Size = New-Object System.Drawing.Size(220, 55)
$form.Controls.Add($button)

$script:Done = $false
$script:OpenUrl = $null

function Step([string]$text) {
    $progress.Value = [Math]::Min($progress.Value + 1, $progress.Maximum)
    $status.Text = $text
    Write-Log $text
    [System.Windows.Forms.Application]::DoEvents()
}

function Install-ShweMuse {
    $button.Enabled = $false
    $progress.Visible = $true
    $progress.Value = 0

    if (-not (Test-Path "$Payload\python\python.exe")) { throw "Setup files not found next to this installer ($Payload). Please unzip the whole folder first." }
    $old = Read-Config
    if ($old.ContainsKey('SITE') -and $old['SITE'] -and $old['SITE'].ToLower() -ne $Site) {
        throw "ဒီ computer က $($old['SITE']) server ပါ — $SiteName ZIP ကို ဒီမှာ မထည့်ပါနဲ့။ $($old['SITE']) ZIP ကို သုံးပါ။"
    }

    Step 'ယခင် Shwe Muse ကို ရပ်နေပါတယ်...'
    Stop-ShweMuse
    New-Item -ItemType Directory -Force -Path $Root, "$Root\data", "$Root\backups", "$Root\logs" | Out-Null

    Step 'ဖိုင်တွေ ကူးနေပါတယ် (၁–၂ မိနစ်)...'
    Copy-Folder "$Payload\python" "$Root\python"
    Copy-Folder "$Payload\app" "$Root\app"
    Get-ChildItem -Path "$Root\python", "$Root\app" -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

    # Data from before the go-live (test data, or the first installer's cloud copy):
    # set aside once (kept in backups) so the site starts fresh. See fresh_check.py.
    $db = "$Root\data\shwemuse.db"
    if (Test-Path $db) {
        $check = & "$Root\python\python.exe" "$Root\app\fresh_check.pyc" $db $Site
        if ("$check".Trim() -eq 'reset') {
            Remove-Item "$Root\data\sync_state.json" -ErrorAction SilentlyContinue
            Write-Log 'test data cleared for go-live (backup saved in backups\before-reset_*.db)'
        }
        if ("$check".Trim() -eq 'old') {
            $aside = "$Root\backups\before-fresh-start_{0:yyyyMMdd_HHmmss}" -f (Get-Date)
            New-Item -ItemType Directory -Force -Path $aside | Out-Null
            Move-Item "$Root\data\shwemuse.db*" $aside -Force
            Remove-Item "$Root\data\sync_state.json" -ErrorAction SilentlyContinue
            Write-Log "old database moved to $aside (site starts fresh)"
        }
    }

    # Tara item list + doctors, loaded once per database (see import_catalog.py).
    if (Test-Path $db) {
        $imp = & "$Root\python\python.exe" "$Root\app\import_catalog.pyc" $db "$Root\app\tara_catalog.json"
        Write-Log "item list: $imp"
    }

    Step 'Setting ပြင်နေပါတယ်...'
    $cfg = Read-Config
    if ($cfg.ContainsKey('PORT')) {
        $port = [int]$cfg['PORT']
    } else {
        $busy = Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue
        $port = if ($busy) { 8080 } else { 80 }
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $secret = [Convert]::ToBase64String($bytes)
        @(
            '# Shwe Muse offline server settings',
            "PORT=$port",
            "SECRET_KEY=$secret",
            '# Extra backup copy (USB / second drive), several separated by ;  e.g. D:\ShweMuseBackup',
            'BACKUP_COPY_TO='
        ) | Set-Content -Path "$Root\config.env" -Encoding ASCII
    }
    # Cloud sync settings come with each installer version (replaced on update).
    $cloudEnv = Join-Path $PSScriptRoot 'cloud.env'
    $keep = Get-Content "$Root\config.env" | Where-Object { $_ -notmatch '^\s*(CLOUD_URL|SYNC_KEY|SITE)\s*=' }
    $add = @("SITE=$Site")
    if (Test-Path $cloudEnv) { $add += Get-Content $cloudEnv }
    ($keep + $add) | Set-Content -Path "$Root\config.env" -Encoding ASCII

    Step 'Firewall ဖွင့်နေပါတယ်...'
    Remove-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -Profile Any | Out-Null

    Step 'Computer ဖွင့်တိုင်း အလိုလို စအောင် လုပ်နေပါတယ်...'
    $pyw = "$Root\python\pythonw.exe"
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
    $triggers = @(New-ScheduledTaskTrigger -AtStartup)
    try {
        # Every 5 minutes: starts the server again if it ever stopped (ignored while it runs).
        $triggers += New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration ([TimeSpan]::FromDays(3650))
    } catch { Write-Log "watchdog trigger skipped: $_" }
    $action = New-ScheduledTaskAction -Execute $pyw -Argument "`"$Root\app\server.pyc`"" -WorkingDirectory "$Root\app"
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Principal $principal -Settings $settings -Force | Out-Null

    $bAction = New-ScheduledTaskAction -Execute $pyw -Argument "`"$Root\app\backup.pyc`"" -WorkingDirectory "$Root\app"
    $bTriggers = @((New-ScheduledTaskTrigger -Daily -At '13:00'), (New-ScheduledTaskTrigger -Daily -At '20:30'))
    $bSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $BackupTask -Action $bAction -Trigger $bTriggers -Principal $principal -Settings $bSettings -Force | Out-Null

    # Cloud sync runs inside the server itself (every 15 seconds); remove the
    # separate task an older installer version created.
    Unregister-ScheduledTask -TaskName $SyncTask -Confirm:$false -ErrorAction SilentlyContinue

    # A server must not fall asleep.
    powercfg /change standby-timeout-ac 0 | Out-Null
    powercfg /change hibernate-timeout-ac 0 | Out-Null

    Step 'Shwe Muse ကို စနေပါတယ်...'
    Start-ScheduledTask -TaskName $TaskName
    $ok = $false
    for ($i = 0; $i -lt 90; $i++) {
        try {
            $r = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$port/health" -TimeoutSec 3
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { }
        Start-Sleep -Seconds 1
        [System.Windows.Forms.Application]::DoEvents()
    }
    if (-not $ok) { throw "Server did not start. See $Root\logs\server.log" }

    Step 'Desktop shortcut လုပ်နေပါတယ်...'
    $desktop = [Environment]::GetFolderPath('CommonDesktopDirectory')
    $icon = "$Root\app\shwemuse.ico"
    $local = Get-Url 'localhost' $port
    $edge = Get-EdgePath
    if ($edge) {
        New-Shortcut "$desktop\Shwe Muse.lnk" $edge "--app=$local" $icon
    } else {
        Set-Content -Path "$desktop\Shwe Muse.url" -Value "[InternetShortcut]`r`nURL=$local/" -Encoding ASCII
    }

    Step 'တခြား computer တွေအတွက် shortcut ပြင်နေပါတယ်...'
    $ips = @(Get-LanIPs)
    $mainIp = if ($ips.Count -gt 0) { $ips[0] } else { 'localhost' }
    $lanUrl = Get-Url $mainIp $port
    $share = "$Root\For-Other-Computers"
    New-Item -ItemType Directory -Force -Path $share | Out-Null
    Copy-Item $icon "$share\shwemuse.ico" -Force
    Set-Content -Path "$share\Shwe Muse.url" -Value "[InternetShortcut]`r`nURL=$lanUrl/" -Encoding ASCII
    @(
        '@echo off',
        'rem Makes a "Shwe Muse" app shortcut on this computer''s desktop.',
        "set URL=$lanUrl",
        'set ICON=%PUBLIC%\shwemuse.ico',
        'copy /Y "%~dp0shwemuse.ico" "%ICON%" >nul 2>&1',
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "$e=@((Join-Path ${env:ProgramFiles(x86)} ''Microsoft\Edge\Application\msedge.exe''),(Join-Path $env:ProgramFiles ''Microsoft\Edge\Application\msedge.exe''))|?{Test-Path $_}|select -First 1; $s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath(''Desktop'')+''\Shwe Muse.lnk''); if($e){$s.TargetPath=$e; $s.Arguments=''--app=%URL%''}else{$s.TargetPath=''%URL%''}; $s.IconLocation=$env:ICON; $s.Save()"',
        'echo Shwe Muse shortcut is on the desktop: %URL%',
        'pause'
    ) | Set-Content -Path "$share\Make Shwe Muse shortcut.bat" -Encoding ASCII

    Step 'ပြီးပါပြီ'
    $script:Done = $true
    $script:OpenUrl = $local
    $addr = ($ips | ForEach-Object { Get-Url $_ $port }) -join "`r`n"
    if (-not $addr) { $addr = '(LAN ကြိုး မချိတ်ရသေးပါ)' }
    $info.Text = "✓ Install ပြီးပါပြီ — Shwe Muse အလုပ်လုပ်နေပါပြီ။`r`n`r`n" +
                 "တခြား computer တွေကနေ ဒီလိပ်စာနဲ့ ဖွင့်ပါ:`r`n$addr`r`n`r`n" +
                 "Shortcut: C:\ShweMuse\For-Other-Computers ကို USB နဲ့ ကူးသွားပါ"
    $info.ForeColor = $Green
    $status.Text = 'Router မှာ ဒီ computer ကို IP ပုံသေ (DHCP reservation) ပေးထားရင် IP မပြောင်းတော့ပါ။'
    $button.Text = 'Shwe Muse ဖွင့်မယ်'
    $button.Font = New-Object System.Drawing.Font($MmFont, 13, [System.Drawing.FontStyle]::Bold)
    $button.Enabled = $true
    Write-Log "installed OK — $addr"
}

$button.Add_Click({
    if ($script:Done) {
        $edge = Get-EdgePath
        if ($edge) { Start-Process $edge "--app=$($script:OpenUrl)" } else { Start-Process $script:OpenUrl }
        $form.Close()
        return
    }
    try {
        Install-ShweMuse
    } catch {
        Write-Log "ERROR: $_"
        $status.Text = ''
        $button.Enabled = $true
        [System.Windows.Forms.MessageBox]::Show("Install မအောင်မြင်ပါ:`r`n`r`n$_`r`n`r`nLog: $Root\logs\install.log", 'Shwe Muse', 'OK', 'Error') | Out-Null
    }
})

[void]$form.ShowDialog()
