param(
    [ValidateSet('store', 'admin')][string]$View = 'store',
    [switch]$NoBrowser,
    [switch]$Stop,
    [ValidateSet('disabled', 'groq')][string]$AIProvider = 'disabled'
)
$ErrorActionPreference = 'Stop'
try {
    $projectRoot = $PSScriptRoot
    $backendPath = Join-Path $projectRoot 'backend'
    $pythonPath = Join-Path $backendPath '.venv\Scripts\python.exe'
    $databasePath = Join-Path $backendPath 'sneaker_store.db'
    $baseUrl = 'http://127.0.0.1:8002'
    function Test-SneakChatServerProcess($Process) {
        $command = $Process.CommandLine
        return ($Process -and $Process.Name -eq 'python.exe' -and $command -like '*uvicorn app.main:app*' -and
            $command -match '(?:^|\s)--port\s+8002(?:\s|$)' -and
            $command -match '(?:^|\s)--host\s+127\.0\.0\.1(?:\s|$)' -and
            $command -match '(?:^|\s)--lifespan\s+off(?:\s|$)' -and
            $command -match ('(?:^|\s)--app-dir\s+"' + [regex]::Escape($backendPath) + '"(?:\s|$)'))
    }
    # A short named mutex serializes launches from both desktop shortcuts.
    $launchMutex = New-Object System.Threading.Mutex($false, 'Local\SneakChatCanonical8002')
    if (-not $launchMutex.WaitOne(30000)) { throw 'SneakChat launch is already in progress.' }
    try {
        $listener = @(Get-NetTCPConnection -LocalPort 8002 -State Listen -ErrorAction SilentlyContinue)
        if ($listener.Count -gt 0) {
            if (-not $Stop -and $AIProvider -ne 'disabled') {
                Write-Warning 'An existing server is kept running. AIProvider applies only when starting a new server.'
            }
            foreach ($connection in $listener) {
                $serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)"
                if (-not (Test-SneakChatServerProcess $serverProcess)) {
                    throw 'Port 8002 belongs to an unrecognized server. No process was stopped.'
                }
            }
        }
        if ($Stop) {
            $processes = @(Get-CimInstance Win32_Process)
            $servers = @($processes | Where-Object { Test-SneakChatServerProcess $_ })
            $serverIds = @($servers | Select-Object -ExpandProperty ProcessId)
            $roots = @($servers | Where-Object { $_.ParentProcessId -notin $serverIds })
            $treeIds = @($serverIds)
            do {
                $children = @($processes | Where-Object { $_.ParentProcessId -in $treeIds -and $_.ProcessId -notin $treeIds })
                $treeIds += @($children | Select-Object -ExpandProperty ProcessId)
            } while ($children.Count -gt 0)
            foreach ($root in $roots) {
                $current = Get-CimInstance Win32_Process -Filter "ProcessId=$($root.ProcessId)"
                if (-not $current) { continue }
                if ($current.CreationDate -ne $root.CreationDate -or -not (Test-SneakChatServerProcess $current)) {
                    throw 'Process identity changed. Stop cancelled.'
                }
                # Windows venv Python has a launcher parent and a listening child.
                # Terminate the verified root tree, not only the port owner.
                & "$env:SystemRoot\System32\taskkill.exe" /PID $root.ProcessId /T /F | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'SneakChat process tree could not be stopped.' }
            }
            for ($attempt = 0; $attempt -lt 20; $attempt++) {
                $remaining = if ($treeIds.Count) { @(Get-Process -Id $treeIds -ErrorAction SilentlyContinue) } else { @() }
                $listening = @(Get-NetTCPConnection -LocalPort 8002 -State Listen -ErrorAction SilentlyContinue)
                if (-not $remaining -and -not $listening) { break }
                Start-Sleep -Milliseconds 250
            }
            if ($remaining -or $listening) { throw 'SneakChat stop did not complete; processes or listener remain.' }
            Write-Output 'SneakChat stopped. No server processes remain. In-flight requests are interrupted.'
            return
        }
        foreach ($required in @($pythonPath, $databasePath, (Join-Path $projectRoot 'frontend\dist\index.html'))) {
            if (-not (Test-Path -LiteralPath $required -PathType Leaf)) { throw "Missing required file: $required" }
        }
        if ($listener.Count -eq 0) {
            $env:SNEAKCHAT_DATABASE_URL = 'sqlite:///' + ($databasePath -replace '\\', '/')
            $env:AI_PROVIDER = $AIProvider
            $logPath = Join-Path $projectRoot 'outputs'
            New-Item -ItemType Directory -Path $logPath -Force | Out-Null
            $arguments = '-m uvicorn app.main:app --app-dir "' + $backendPath + '" --host 127.0.0.1 --port 8002 --lifespan off'
            # The established schema/catalog is used as-is: no startup seeding/migration.
            $previousGroqKey = $env:GROQ_API_KEY
            $secureGroqKey = $null
            $keyPointer = [IntPtr]::Zero
            try {
                if ($AIProvider -eq 'groq') {
                    . (Join-Path $projectRoot 'scripts\GroqKey.ps1')
                    $secureGroqKey = Get-SneakChatGroqKey
                    if ($secureGroqKey) {
                        $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureGroqKey)
                        $env:GROQ_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
                    } else {
                        Write-Warning 'Groq key is not configured. Chat will use fallback.'
                    }
                }
                Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $backendPath -WindowStyle Hidden `
                    -RedirectStandardOutput (Join-Path $logPath 'server.stdout.log') `
                    -RedirectStandardError (Join-Path $logPath 'server.stderr.log') | Out-Null
            } finally {
                $env:GROQ_API_KEY = $previousGroqKey
                $previousGroqKey = $null
                if ($keyPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer) }
                if ($secureGroqKey) { $secureGroqKey.Dispose() }
            }
        }
        $ready = $false
        for ($attempt = 0; $attempt -lt 40; $attempt++) {
            try {
                $health = Invoke-RestMethod -Uri "$baseUrl/api/health" -TimeoutSec 2
                if ($health.status -eq 'ok') { $ready = $true; break }
            } catch { }
            Start-Sleep -Milliseconds 250
        }
        if (-not $ready) { throw "SneakChat did not start. See outputs/server.stderr.log in $projectRoot" }
    } finally {
        $launchMutex.ReleaseMutex()
        $launchMutex.Dispose()
    }
    $targetUrl = if ($View -eq 'admin') { "$baseUrl/admin" } else { "$baseUrl/" }
    Write-Output $targetUrl
    if (-not $NoBrowser) { Start-Process $targetUrl }
} catch {
    if (-not $NoBrowser) {
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'SneakChat') | Out-Null
    }
    throw
}
