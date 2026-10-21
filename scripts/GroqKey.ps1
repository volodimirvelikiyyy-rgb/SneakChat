# Windows-only, per-user DPAPI storage. Never write the decrypted key to output.
function Get-SneakChatGroqKeyPath {
    Join-Path $env:LOCALAPPDATA 'SneakChat\groq_key.dpapi'
}

function Get-SneakChatGroqKey {
    if (-not [string]::IsNullOrWhiteSpace($env:GROQ_API_KEY)) {
        return (ConvertTo-SecureString -String $env:GROQ_API_KEY -AsPlainText -Force)
    }
    $path = Get-SneakChatGroqKeyPath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try {
        return (ConvertTo-SecureString -String ([IO.File]::ReadAllText($path)) -ErrorAction Stop)
    } catch {
        Write-Warning 'Protected Groq key cannot be opened by this Windows user. Fallback will be used.'
        return $null
    }
}

function Set-SneakChatGroqKey {
    param([Parameter(Mandatory)][Security.SecureString]$Key)
    $pointer = [IntPtr]::Zero
    try {
        $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Key)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
        if ($plain -notmatch '^gsk_[A-Za-z0-9_-]{16,252}$') {
            throw 'Invalid Groq key format.'
        }
    } finally {
        $plain = $null
        if ($pointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
    }
    $path = Get-SneakChatGroqKeyPath
    $directory = [IO.Path]::GetDirectoryName($path)
    [void][IO.Directory]::CreateDirectory($directory)
    $temporary = Join-Path $directory ('groq-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $cipher = ConvertFrom-SecureString -SecureString $Key -ErrorAction Stop
        [IO.File]::WriteAllText($temporary, $cipher)
        $acl = New-Object Security.AccessControl.FileSecurity
        $acl.SetAccessRuleProtection($true, $false)
        $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl.SetOwner($owner)
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($owner, 'FullControl', 'Allow')))
        $system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($system, 'FullControl', 'Allow')))
        Set-Acl -LiteralPath $temporary -AclObject $acl -ErrorAction Stop
        Move-Item -LiteralPath $temporary -Destination $path -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}
