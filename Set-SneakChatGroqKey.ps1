param([switch]$Dialog)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'scripts\GroqKey.ps1')

if (-not $Dialog) {
    $secureKey = Read-Host 'Groq API key (hidden; never paste it into Codex chat)' -AsSecureString
    try {
        Set-SneakChatGroqKey -Key $secureKey
        Write-Host 'Groq key saved with Windows DPAPI outside the project. No API request was sent.'
    } catch {
        Write-Host 'Key was not saved. Check its format and Windows profile permissions.'
        exit 1
    } finally { $secureKey.Dispose() }
    exit 0
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object Windows.Forms.Form
$form.Text = 'SneakChat — локальная настройка Groq'
$form.ClientSize = New-Object Drawing.Size(560, 265)
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$label = New-Object Windows.Forms.Label
$label.Location = New-Object Drawing.Point(18, 16)
$label.Size = New-Object Drawing.Size(520, 78)
$label.Text = "Введите GROQ_API_KEY здесь, не в чат Codex.`nХранение: зашифрованный файл в профиле Windows, вне проекта.`nМодель: openai/gpt-oss-20b. Сохранение не отправляет запрос к API."
$form.Controls.Add($label)
$keyInput = New-Object Windows.Forms.TextBox
$keyInput.Location = New-Object Drawing.Point(18, 100)
$keyInput.Size = New-Object Drawing.Size(520, 30)
$keyInput.UseSystemPasswordChar = $true
$keyInput.MaxLength = 256
$form.Controls.Add($keyInput)
$status = New-Object Windows.Forms.Label
$status.Location = New-Object Drawing.Point(18, 143)
$status.Size = New-Object Drawing.Size(520, 45)
$status.Text = 'Ключ не попадёт в Git, frontend или логи. Получить ключ: console.groq.com.'
$form.Controls.Add($status)
$save = New-Object Windows.Forms.Button
$save.Text = 'Сохранить ключ'
$save.Location = New-Object Drawing.Point(200, 204)
$save.Size = New-Object Drawing.Size(160, 36)
$cancel = New-Object Windows.Forms.Button
$cancel.Text = 'Отмена'
$cancel.Location = New-Object Drawing.Point(375, 204)
$cancel.Size = New-Object Drawing.Size(160, 36)
$cancel.DialogResult = [Windows.Forms.DialogResult]::Cancel
$form.Controls.AddRange(@($save, $cancel))
$form.AcceptButton = $save
$form.CancelButton = $cancel
$save.Add_Click({
    $secureKey = $null
    try {
        $secureKey = ConvertTo-SecureString -String $keyInput.Text.Trim() -AsPlainText -Force
        Set-SneakChatGroqKey -Key $secureKey
        $keyInput.Clear()
        $form.DialogResult = [Windows.Forms.DialogResult]::OK
        $form.Close()
    } catch {
        $status.Text = 'Ключ не сохранён. Проверьте формат gsk_… и доступ к профилю Windows.'
    } finally {
        if ($secureKey) { $secureKey.Dispose() }
    }
})
$form.Add_Shown({ $keyInput.Focus() })
try { [void]$form.ShowDialog() } finally { $keyInput.Clear(); $form.Dispose() }
