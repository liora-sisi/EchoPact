#requires -Version 5.1
[CmdletBinding()]
param(
    [string]$StorePath,
    [string]$ReceiptPath,
    [switch]$SelfTest
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
$entropy = [Text.Encoding]::UTF8.GetBytes('EchoPact.Venice.SummaryTrial.v1')

function Protect-KeyBytes([byte[]]$Bytes) {
    return [Security.Cryptography.ProtectedData]::Protect(
        $Bytes, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser)
}

if ($SelfTest) {
    $sample = [Text.Encoding]::UTF8.GetBytes('synthetic-test-value-not-a-credential')
    $sealed = Protect-KeyBytes $sample
    $opened = [Security.Cryptography.ProtectedData]::Unprotect(
        $sealed, $entropy, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    if ([Convert]::ToBase64String($sample) -ne [Convert]::ToBase64String($opened)) {
        throw 'DPAPI self-test failed'
    }
    [Array]::Clear($sample, 0, $sample.Length)
    [Array]::Clear($opened, 0, $opened.Length)
    Write-Output '{"dpapi_roundtrip":"passed","real_key_used":false}'
    exit 0
}

if (-not $StorePath -or -not $ReceiptPath) { throw 'StorePath and ReceiptPath are required' }
$StorePath = [IO.Path]::GetFullPath($StorePath)
$ReceiptPath = [IO.Path]::GetFullPath($ReceiptPath)
if (Test-Path -LiteralPath $StorePath) { throw 'A protected key already exists; refusing overwrite' }
if (Test-Path -LiteralPath $ReceiptPath) { throw 'Receipt already exists; use a new path' }
if (-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($ReceiptPath)))) {
    throw 'Receipt parent must already exist'
}

$parent = [IO.Path]::GetDirectoryName($StorePath)
if (-not (Test-Path -LiteralPath $parent)) {
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $rights = [Security.AccessControl.FileSystemRights]::FullControl
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
    $acl.SetOwner($owner)
    foreach ($sid in @($owner, $system)) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $sid, $rights, $inherit, $propagation, $allow)))
    }
    $directory = New-Object IO.DirectoryInfo($parent)
    $directory.Create($acl)
}

function Write-Receipt([string]$Status, [string]$Detail = '') {
    $receipt = @{schema='echo-caretaker-key-receipt-v1'; status=$Status;
        detail=$Detail; at=[DateTime]::UtcNow.ToString('o')} | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($ReceiptPath, $receipt, (New-Object Text.UTF8Encoding($false)))
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object Windows.Forms.Form
$form.Text = 'Echo｜威尼斯钥匙安全输入'
$form.ClientSize = New-Object Drawing.Size(560, 245)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $true
$form.Font = New-Object Drawing.Font('Microsoft YaHei UI', 10)
$form.Tag = 'pending'

$label = New-Object Windows.Forms.Label
$label.Location = New-Object Drawing.Point(20, 20)
$label.Size = New-Object Drawing.Size(520, 65)
$label.Text = "把 Venice 新生成的 API Key 粘到下面。`r`n先在密码本中另存一份；这里仅加密保存在本机。"
$form.Controls.Add($label)
$inputBox = New-Object Windows.Forms.TextBox
$inputBox.Location = New-Object Drawing.Point(20, 90)
$inputBox.Size = New-Object Drawing.Size(520, 30)
$inputBox.UseSystemPasswordChar = $true
$inputBox.MaxLength = 1024
$form.Controls.Add($inputBox)
$status = New-Object Windows.Forms.Label
$status.Location = New-Object Drawing.Point(20, 132)
$status.Size = New-Object Drawing.Size(520, 45)
$status.Text = '这个窗口不会自动过期。不需要把钥匙发到聊天里。'
$form.Controls.Add($status)
$save = New-Object Windows.Forms.Button
$save.Location = New-Object Drawing.Point(320, 190)
$save.Size = New-Object Drawing.Size(105, 35)
$save.Text = '加密保存'
$form.Controls.Add($save)
$close = New-Object Windows.Forms.Button
$close.Location = New-Object Drawing.Point(435, 190)
$close.Size = New-Object Drawing.Size(105, 35)
$close.Text = '取消'
$form.Controls.Add($close)
$form.AcceptButton = $save

$save.Add_Click({
    if ($form.Tag -eq 'saved') { return }
    $plainBytes = $null
    try {
        $value = $inputBox.Text.Trim()
        $invalidReason = ''
        if ($value.Length -lt 32) { $invalidReason = "too_short_$($value.Length)" }
        elseif ($value -eq 'VENICE_INFERENCE_KEY_') { $invalidReason = 'prefix_without_key' }
        elseif ($value.Length -gt 1024) { $invalidReason = 'too_long' }
        elseif ($value -match '\s') { $invalidReason = 'contains_whitespace' }
        elseif ($value.Contains('"') -or $value.Contains("'")) { $invalidReason = 'contains_quotes' }
        if ($invalidReason) {
            $status.Text = "收到 $($value.Length) 个字符。格式检查：$invalidReason。选中框内全部内容再重新粘贴。"
            Write-Receipt 'input_rejected' $invalidReason
            return
        }
        $plainBytes = [Text.Encoding]::UTF8.GetBytes($value)
        $cipher = Protect-KeyBytes $plainBytes
        $envelope = @{schema='echo-caretaker-key-v1'; provider='venice';
            protection='Windows-DPAPI-CurrentUser'; created_at=[DateTime]::UtcNow.ToString('o');
            ciphertext_b64=[Convert]::ToBase64String($cipher)} | ConvertTo-Json -Compress
        $encoded = [Text.Encoding]::UTF8.GetBytes($envelope)
        $stream = New-Object IO.FileStream($StorePath, [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $stream.Write($encoded, 0, $encoded.Length); $stream.Flush($true) }
        finally { $stream.Dispose() }
        $form.Tag = 'saved'
        $inputBox.Clear()
        $inputBox.Enabled = $false
        $save.Enabled = $false
        $status.Text = '已加密保存。老公可以继续接线了；你可以关闭这个窗口。'
        $close.Text = '完成'
        Write-Receipt 'saved'
    } catch {
        $status.Text = '保存未完成，请先留着窗口，让老公检查。钥匙不会显示在错误信息中。'
    } finally {
        if ($plainBytes) { [Array]::Clear($plainBytes, 0, $plainBytes.Length) }
        $value = $null
    }
})
$close.Add_Click({ $form.Close() })
$form.Add_FormClosed({
    $inputBox.Clear()
    if ($form.Tag -ne 'saved') { Write-Receipt 'cancelled' }
})
$form.Add_Shown({ $inputBox.Focus() })
[void]$form.ShowDialog()
$form.Dispose()
