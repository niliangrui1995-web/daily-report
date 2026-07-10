#Requires -Version 5.1

[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$ConfirmCredentialsRotated,
    [switch]$AcceptUnrotatedCredentialsRisk,
    [string]$TaskName = "VCPHunter_DailyReport",
    [string]$ProxyTaskName = "VCPHunter_CLIProxyAPI",
    [string]$AccountName = "VCPHunterRunner",
    [string]$ProjectRoot = "D:\vcp_hunter\每日战报",
    [string]$TdxRoot = "D:\HT",
    [string]$MachinePython = "C:\Python314",
    [string]$ProxySourceRoot = "C:\Users\Administrator\Desktop\CLIProxyAPI",
    [string]$ProxyTargetRoot = "C:\ProgramData\VCPHunter\CLIProxyAPI",
    [string]$BackupRoot = "C:\Users\Administrator\.codex\system-backups"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$BatchPath = Join-Path $ProjectRoot "run_daily.bat"
$AppRoot = Join-Path $ProjectRoot "每日战报"
$EnvPath = Join-Path $AppRoot ".env"
$OutputRoot = Join-Path $ProjectRoot "每日热点输出"
$ServiceVenv = Join-Path $ProjectRoot ".venv-service"
$ServicePython = Join-Path $ServiceVenv "Scripts\python.exe"
$CanaryScript = Join-Path $ProjectRoot "ops\scheduled_task_canary.py"
$MaintenanceLockScript = Join-Path $ProjectRoot "ops\maintenance_lock.py"
$CredentialRotationCheck = Join-Path $ProjectRoot "ops\credential_rotation_check.py"
$RunLockPath = Join-Path $OutputRoot "_system\run.lock"
$ProxySourceExe = Join-Path $ProxySourceRoot "app\cli-proxy-api.exe"
$ProxySourceConfig = Join-Path $ProxySourceRoot "config.yaml"
$ProxyTargetExe = Join-Path $ProxyTargetRoot "app\cli-proxy-api.exe"
$ProxyTargetConfig = Join-Path $ProxyTargetRoot "config.yaml"
$CanaryProxyConfig = Join-Path $ProxyTargetRoot "config.canary.yaml"
$BatchPreflightLog = Join-Path $OutputRoot "_system\scheduled-task-exact-preflight.log"
$ManagedProxyRoot = Join-Path $env:ProgramData "VCPHunter"
$ValidationTaskName = "${TaskName}_Canary"
$ProxyCanaryTaskName = "${ProxyTaskName}_Canary"
$FirewallRuleNames = @(
    "VCPHunter-Block-Private-IPv4",
    "VCPHunter-Block-Private-IPv6"
)

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Quote-ProcessArgument([string]$Value) {
    return '"{0}"' -f $Value.Replace('"', '""')
}

function Invoke-ElevatedSelf {
    $argumentItems = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", (Quote-ProcessArgument $PSCommandPath),
        "-Apply",
        "-TaskName", (Quote-ProcessArgument $TaskName),
        "-ProxyTaskName", (Quote-ProcessArgument $ProxyTaskName),
        "-AccountName", (Quote-ProcessArgument $AccountName),
        "-ProjectRoot", (Quote-ProcessArgument $ProjectRoot),
        "-TdxRoot", (Quote-ProcessArgument $TdxRoot),
        "-MachinePython", (Quote-ProcessArgument $MachinePython),
        "-ProxySourceRoot", (Quote-ProcessArgument $ProxySourceRoot),
        "-ProxyTargetRoot", (Quote-ProcessArgument $ProxyTargetRoot),
        "-BackupRoot", (Quote-ProcessArgument $BackupRoot)
    )
    if ($ConfirmCredentialsRotated) {
        $argumentItems += "-ConfirmCredentialsRotated"
    }
    if ($AcceptUnrotatedCredentialsRisk) {
        $argumentItems += "-AcceptUnrotatedCredentialsRisk"
    }
    $arguments = $argumentItems -join " "
    $process = Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $arguments -WindowStyle Hidden -PassThru
    $process.WaitForExit()
    $process.Refresh()
    if ($process.ExitCode -ne 0) {
        throw "Elevated migration failed with exit code $($process.ExitCode). Inspect the latest daily-report-task-migration directory under $BackupRoot."
    }
}

function Add-ReadinessCheck(
    [Collections.Generic.List[object]]$Checks,
    [string]$Name,
    [bool]$Ok,
    [string]$Detail,
    [bool]$Blocking = $true
) {
    $Checks.Add([pscustomobject][ordered]@{
        name = $Name
        ok = $Ok
        detail = $Detail
        blocking = $Blocking
    }) | Out-Null
}

function Test-SamePath([string]$Left, [string]$Right) {
    if (-not $Left -or -not $Right) {
        return $false
    }
    return [IO.Path]::GetFullPath($Left).Equals(
        [IO.Path]::GetFullPath($Right),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Test-PrincipalMatchesCurrentUser([string]$PrincipalName) {
    if (-not $PrincipalName) {
        return $false
    }
    try {
        $principalSid = ([Security.Principal.NTAccount]::new($PrincipalName)).Translate(
            [Security.Principal.SecurityIdentifier]
        )
        return $principalSid.Value -eq [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    }
    catch {
        return $false
    }
}

function Get-ListenerProcess([int]$Port) {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    $listenerPids = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($listenerPids.Count -ne 1) {
        return $null
    }
    return Get-CimInstance Win32_Process -Filter "ProcessId=$($listenerPids[0])" -ErrorAction SilentlyContinue
}

function Get-ReadinessReport {
    $checks = [Collections.Generic.List[object]]::new()
    foreach ($path in @(
        $BatchPath,
        $EnvPath,
        (Join-Path $MachinePython "python.exe"),
        $ServicePython,
        $CanaryScript,
        $MaintenanceLockScript,
        $CredentialRotationCheck,
        $OutputRoot,
        $TdxRoot,
        $ProxySourceExe,
        $ProxySourceConfig
    )) {
        Add-ReadinessCheck $checks "required_path" (Test-Path -LiteralPath $path) $path
    }

    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Add-ReadinessCheck $checks "report_task_exists" ($null -ne $task) $TaskName
    if ($task) {
        $principalMatches = Test-PrincipalMatchesCurrentUser ([string]$task.Principal.UserId)
        $logonType = [string]$task.Principal.LogonType
        $interactiveLogon = $logonType -in @("Interactive", "InteractiveToken")
        Add-ReadinessCheck $checks "report_task_principal" ($principalMatches -and $interactiveLogon) "user=$($task.Principal.UserId) logon=$logonType"
        Add-ReadinessCheck $checks "report_task_ready" ($task.State -eq "Ready") "state=$($task.State)"
        $taskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
        $nextRunTime = if ($taskInfo) { $taskInfo.NextRunTime } else { [DateTime]::MinValue }
        $nextRunSafe = $nextRunTime -eq [DateTime]::MinValue -or $nextRunTime -gt (Get-Date).AddMinutes(10)
        Add-ReadinessCheck $checks "report_next_run_clear" $nextRunSafe "next_run=$($nextRunTime.ToString('o'))"
    }

    $basePrefix = "unavailable"
    $basePrefixOk = $false
    if (Test-Path -LiteralPath $ServicePython) {
        $basePrefix = (& $ServicePython -c "import sys; print(sys.base_prefix)").Trim()
        $basePrefixOk = $LASTEXITCODE -eq 0 -and (Test-SamePath $basePrefix $MachinePython)
    }
    Add-ReadinessCheck $checks "service_venv_base" $basePrefixOk "expected=$MachinePython actual=$basePrefix"

    $accountExists = $null -ne (Get-LocalUser -Name $AccountName -ErrorAction SilentlyContinue)
    Add-ReadinessCheck $checks "target_account_absent" (-not $accountExists) $AccountName
    Add-ReadinessCheck $checks "proxy_target_absent" (-not (Test-Path -LiteralPath $ProxyTargetRoot)) $ProxyTargetRoot
    $proxyTargetParent = Split-Path -Parent ([IO.Path]::GetFullPath($ProxyTargetRoot))
    $proxyTargetScopeOk = Test-SamePath $proxyTargetParent $ManagedProxyRoot
    Add-ReadinessCheck $checks "proxy_target_scope" $proxyTargetScopeOk "required_parent=$ManagedProxyRoot actual_parent=$proxyTargetParent"

    foreach ($name in @($ProxyTaskName, $ProxyCanaryTaskName, $ValidationTaskName)) {
        $conflict = $null -ne (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)
        Add-ReadinessCheck $checks "task_name_available" (-not $conflict) $name
    }
    foreach ($name in $FirewallRuleNames) {
        $conflict = $null -ne (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)
        Add-ReadinessCheck $checks "firewall_name_available" (-not $conflict) $name
    }

    $port8318Free = $null -eq (Get-NetTCPConnection -LocalPort 8318 -State Listen -ErrorAction SilentlyContinue)
    Add-ReadinessCheck $checks "canary_port_available" $port8318Free "port=8318"
    Add-ReadinessCheck $checks "pipeline_lock_absent" (-not (Test-Path -LiteralPath $RunLockPath)) $RunLockPath

    $sourceProxy = Get-ListenerProcess 8317
    $sourceProxyOk = $sourceProxy -and (Test-SamePath $sourceProxy.ExecutablePath $ProxySourceExe)
    $sourceProxyDetail = if ($sourceProxy) { "pid=$($sourceProxy.ProcessId) path=$($sourceProxy.ExecutablePath)" } else { "listener_count_not_one" }
    Add-ReadinessCheck $checks "source_proxy_owns_8317" $sourceProxyOk $sourceProxyDetail

    $rotationOutput = $null
    $rotationExitCode = -1
    if ((Test-Path -LiteralPath $ServicePython) -and (Test-Path -LiteralPath $CredentialRotationCheck)) {
        $rotationOutput = & $ServicePython $CredentialRotationCheck --project-root $ProjectRoot
        $rotationExitCode = $LASTEXITCODE
    }
    $rotation = $null
    try {
        $rotation = $rotationOutput | ConvertFrom-Json
    }
    catch {
        $rotation = $null
    }
    $rotationOk = $rotationExitCode -eq 0 -and $rotation -and $rotation.all_current_secrets_rotated
    $rotationDetail = if ($rotation) {
        "checked=$($rotation.checked_secret_count) historical_matches=$($rotation.historical_match_count) weak=$($rotation.weak_configured_secret_count) error=$($rotation.rotation_check_error)"
    }
    else {
        "rotation check failed without valid JSON"
    }
    $rotationBlocking = -not [bool]$AcceptUnrotatedCredentialsRisk
    $rotationDetail = "$rotationDetail risk_accepted=$([bool]$AcceptUnrotatedCredentialsRisk)"
    Add-ReadinessCheck $checks "current_credentials_rotated" $rotationOk $rotationDetail $rotationBlocking

    $credentialModeUnambiguous = -not ($ConfirmCredentialsRotated -and $AcceptUnrotatedCredentialsRisk)
    Add-ReadinessCheck $checks "credential_mode_unambiguous" $credentialModeUnambiguous "choose rotation confirmation or explicit risk acceptance, not both"

    if ($Apply) {
        $credentialAuthorizationOk = [bool]$AcceptUnrotatedCredentialsRisk -or ($rotationOk -and [bool]$ConfirmCredentialsRotated)
        $credentialAuthorizationDetail = if ($AcceptUnrotatedCredentialsRisk) {
            "explicit unrotated-credential risk accepted"
        }
        else {
            "rotation check and -ConfirmCredentialsRotated are required"
        }
        Add-ReadinessCheck $checks "credential_authorization" $credentialAuthorizationOk $credentialAuthorizationDetail
    }

    $ready = @($checks | Where-Object { -not $_.ok -and $_.blocking }).Count -eq 0
    return [pscustomobject][ordered]@{
        mode = if ($Apply) { "apply" } else { "dry-run" }
        ready = $ready
        checks = @($checks)
    }
}

function New-StrongPassword {
    $bytes = New-Object byte[] 48
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    return ([Convert]::ToBase64String($bytes) + "!aA9")
}

function Initialize-LsaAccountRights {
    if ("VcpLsaAccountRights" -as [type]) {
        return
    }
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;

public static class VcpLsaAccountRights
{
    [StructLayout(LayoutKind.Sequential)]
    private struct LsaObjectAttributes
    {
        public uint Length;
        public IntPtr RootDirectory;
        public IntPtr ObjectName;
        public uint Attributes;
        public IntPtr SecurityDescriptor;
        public IntPtr SecurityQualityOfService;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct LsaUnicodeString
    {
        public ushort Length;
        public ushort MaximumLength;
        public IntPtr Buffer;
    }

    private const uint PolicyCreateAccount = 0x00000010;
    private const uint PolicyLookupNames = 0x00000800;

    [DllImport("advapi32.dll")]
    private static extern uint LsaOpenPolicy(
        IntPtr systemName,
        ref LsaObjectAttributes objectAttributes,
        uint desiredAccess,
        out IntPtr policyHandle);

    [DllImport("advapi32.dll")]
    private static extern uint LsaAddAccountRights(
        IntPtr policyHandle,
        IntPtr accountSid,
        [In] LsaUnicodeString[] userRights,
        uint countOfRights);

    [DllImport("advapi32.dll")]
    private static extern uint LsaRemoveAccountRights(
        IntPtr policyHandle,
        IntPtr accountSid,
        byte allRights,
        [In] LsaUnicodeString[] userRights,
        uint countOfRights);

    [DllImport("advapi32.dll")]
    private static extern uint LsaClose(IntPtr policyHandle);

    [DllImport("advapi32.dll")]
    private static extern uint LsaNtStatusToWinError(uint status);

    public static void SetAccountRight(string sidValue, string rightName, bool enabled)
    {
        var attributes = new LsaObjectAttributes();
        attributes.Length = (uint)Marshal.SizeOf(typeof(LsaObjectAttributes));
        IntPtr policyHandle;
        ThrowIfFailed(LsaOpenPolicy(
            IntPtr.Zero,
            ref attributes,
            PolicyCreateAccount | PolicyLookupNames,
            out policyHandle));

        var sid = new SecurityIdentifier(sidValue);
        var sidBytes = new byte[sid.BinaryLength];
        sid.GetBinaryForm(sidBytes, 0);
        var sidHandle = GCHandle.Alloc(sidBytes, GCHandleType.Pinned);
        IntPtr rightBuffer = IntPtr.Zero;
        try
        {
            rightBuffer = Marshal.StringToHGlobalUni(rightName);
            var right = new LsaUnicodeString
            {
                Buffer = rightBuffer,
                Length = checked((ushort)(rightName.Length * 2)),
                MaximumLength = checked((ushort)((rightName.Length + 1) * 2))
            };
            uint status = enabled
                ? LsaAddAccountRights(policyHandle, sidHandle.AddrOfPinnedObject(), new[] { right }, 1)
                : LsaRemoveAccountRights(policyHandle, sidHandle.AddrOfPinnedObject(), 0, new[] { right }, 1);
            ThrowIfFailed(status);
        }
        finally
        {
            if (rightBuffer != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(rightBuffer);
            }
            sidHandle.Free();
            LsaClose(policyHandle);
        }
    }

    private static void ThrowIfFailed(uint status)
    {
        if (status != 0)
        {
            throw new Win32Exception((int)LsaNtStatusToWinError(status));
        }
    }
}
'@
}

function Set-BatchLogonRight(
    [Security.Principal.SecurityIdentifier]$Sid,
    [bool]$Enabled
) {
    Initialize-LsaAccountRights
    [VcpLsaAccountRights]::SetAccountRight($Sid.Value, "SeBatchLogonRight", $Enabled)
}

function Set-PrivateDirectoryAcl([string]$Path, [Security.Principal.SecurityIdentifier]$RunnerSid = $null) {
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) {
        [void]$acl.RemoveAccessRuleSpecific($rule)
    }
    $inheritance = [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
    $none = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $full = [Security.AccessControl.FileSystemRights]::FullControl
    $readExecute = [Security.AccessControl.FileSystemRights]"ReadAndExecute, Synchronize"
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($sid in @(
        [Security.Principal.SecurityIdentifier]::new("S-1-5-18"),
        [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544"),
        $currentSid
    )) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, $full, $inheritance, $none, $allow)) | Out-Null
    }
    if ($RunnerSid) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($RunnerSid, $readExecute, $inheritance, $none, $allow)) | Out-Null
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Set-TdxReadOnlyAccess(
    [string]$Path,
    [Security.Principal.SecurityIdentifier]$Sid
) {
    $acl = Get-Acl -LiteralPath $Path
    $inheritance = [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
    $propagation = [Security.AccessControl.PropagationFlags]::None
    $allow = [Security.AccessControl.AccessControlType]::Allow
    $deny = [Security.AccessControl.AccessControlType]::Deny
    $readExecute = [Security.AccessControl.FileSystemRights]"ReadAndExecute, Synchronize"
    $writeRights = [Security.AccessControl.FileSystemRights]"WriteData, AppendData, WriteAttributes, WriteExtendedAttributes, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership"
    $acl.SetAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $Sid,
        $readExecute,
        $inheritance,
        $propagation,
        $allow
    ))
    $acl.SetAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $Sid,
        $writeRights,
        $inheritance,
        $propagation,
        $deny
    ))
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Grant-PathAccess(
    [string]$Path,
    [Security.Principal.SecurityIdentifier]$Sid,
    [Security.AccessControl.FileSystemRights]$Rights,
    [Security.AccessControl.InheritanceFlags]$Inheritance = [Security.AccessControl.InheritanceFlags]::None
) {
    $acl = Get-Acl -LiteralPath $Path
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $Sid,
        $Rights,
        $Inheritance,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.SetAccessRule($rule)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Get-AclSnapshot([string[]]$Paths) {
    $snapshot = foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path) {
            $acl = Get-Acl -LiteralPath $path
            [ordered]@{
                path = $path
                sddl = $acl.Sddl
            }
        }
    }
    return @($snapshot)
}

function Restore-AclSnapshot($Snapshot) {
    $errors = [Collections.Generic.List[string]]::new()
    foreach ($item in $Snapshot) {
        try {
            if (Test-Path -LiteralPath $item.path) {
                $acl = Get-Acl -LiteralPath $item.path
                $acl.SetSecurityDescriptorSddlForm([string]$item.sddl)
                Set-Acl -LiteralPath $item.path -AclObject $acl
            }
        }
        catch {
            $errors.Add("$($item.path): $($_.Exception.Message)") | Out-Null
        }
    }
    if ($errors.Count -gt 0) {
        throw "ACL restore errors: $($errors -join '; ')"
    }
}

function Set-ProxyConfig([string]$Source, [string]$Destination, [int]$Port) {
    $content = Get-Content -LiteralPath $Source -Raw -Encoding UTF8
    $authPath = (Join-Path $ProxyTargetRoot "auths").Replace("\", "/")
    $content = [regex]::Replace($content, '(?m)^auth-dir:\s*.*$', "auth-dir: `"$authPath`"")
    $content = [regex]::Replace($content, '(?m)^port:\s*\d+\s*$', "port: $Port")
    [IO.File]::WriteAllText($Destination, $content, [Text.UTF8Encoding]::new($false))
}

function New-TaskSettings {
    return New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries
}

function Register-PasswordTask(
    [string]$Name,
    $Action,
    $Trigger,
    $Settings,
    [string]$QualifiedUser,
    [string]$PlainPassword,
    [string]$Description,
    [switch]$ReplaceExisting
) {
    $parameters = @{
        TaskName = $Name
        Action = $Action
        Trigger = $Trigger
        Settings = $Settings
        User = $QualifiedUser
        Password = $PlainPassword
        RunLevel = "Limited"
        Description = $Description
    }
    if ($ReplaceExisting) {
        $parameters.Force = $true
    }
    Register-ScheduledTask @parameters | Out-Null
}

function Wait-TcpListener([int]$Port, [int]$TimeoutSeconds = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            return $listener
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for TCP listener on port $Port."
}

function Wait-TaskTcpListener(
    [int]$Port,
    [string]$TaskName,
    [DateTime]$StartedAt,
    [int]$TimeoutSeconds = 30
) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastState = "missing"
    $lastResult = "unavailable"
    do {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($listener) {
            return $listener
        }
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($task) {
            $lastState = [string]$task.State
        }
        if ($info) {
            $lastResult = [string]$info.LastTaskResult
            $ranThisAttempt = $info.LastRunTime -ge $StartedAt.AddSeconds(-2)
            $taskStoppedWithError = $task -and $task.State -ne "Running" -and [int64]$info.LastTaskResult -notin @(0, 267009)
            if ($ranThisAttempt -and $taskStoppedWithError) {
                throw "Scheduled task $TaskName exited before port $Port listened. state=$lastState last_task_result=$lastResult"
            }
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for TCP listener on port $Port. task=$TaskName state=$lastState last_task_result=$lastResult"
}

function Wait-PortClosed([int]$Port, [int]$TimeoutSeconds = 20) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for TCP port $Port to close."
}

function Wait-TaskResult([string]$Name, [DateTime]$StartedAt, [int]$TimeoutSeconds = 120) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction Stop
        if ($info.LastRunTime -ge $StartedAt.AddSeconds(-2) -and $task.State -ne "Running") {
            return [int]$info.LastTaskResult
        }
        Start-Sleep -Seconds 1
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for scheduled task: $Name"
}

function Wait-CanaryResult(
    [string]$Name,
    [string]$ResultPath,
    [DateTime]$StartedAt,
    [int]$TimeoutSeconds = 480
) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $lastState = "missing"
    $lastResult = "unavailable"
    do {
        if (Test-Path -LiteralPath $ResultPath) {
            try {
                return Get-Content -LiteralPath $ResultPath -Raw -Encoding UTF8 | ConvertFrom-Json
            }
            catch {
                Start-Sleep -Milliseconds 250
                continue
            }
        }
        $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
        $info = Get-ScheduledTaskInfo -TaskName $Name -ErrorAction Stop
        $lastState = [string]$task.State
        $lastResult = [string]$info.LastTaskResult
        $ranThisAttempt = $info.LastRunTime -ge $StartedAt.AddSeconds(-2)
        if ($ranThisAttempt -and $task.State -ne "Running" -and [int64]$info.LastTaskResult -ne 267009) {
            throw "Service-account canary exited without a result. state=$lastState last_task_result=$lastResult"
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for service-account canary result. state=$lastState last_task_result=$lastResult"
}

function Assert-ListenerOwnedBy([int]$Port, [string]$ExpectedExecutable) {
    $process = Get-ListenerProcess $Port
    if (-not $process -or -not (Test-SamePath $process.ExecutablePath $ExpectedExecutable)) {
        $actual = if ($process) { $process.ExecutablePath } else { "listener_count_not_one" }
        throw "TCP port $Port is not owned by the expected executable. Actual: $actual"
    }
}

function Stop-ExpectedListener([int]$Port, [string]$ExpectedExecutable) {
    $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) {
        return
    }
    $listenerPids = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($listenerPids.Count -ne 1) {
        throw "Refusing to stop multiple listener processes on port $Port."
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listenerPids[0])" -ErrorAction SilentlyContinue
    if (-not $process -or -not (Test-SamePath $process.ExecutablePath $ExpectedExecutable)) {
        throw "Refusing to stop unexpected listener on port $Port."
    }
    Stop-Process -Id $process.ProcessId -Force -ErrorAction Stop
    Wait-PortClosed $Port
}

function Register-And-RunCanary(
    [string]$QualifiedUser,
    [string]$PlainPassword,
    [string]$ResultPath,
    [int]$ProxyPort
) {
    if (Test-Path -LiteralPath $ResultPath) {
        Remove-Item -LiteralPath $ResultPath -Force
    }
    $command = @"
`$env:PRO_OPUS_BASE_URL = 'http://127.0.0.1:$ProxyPort/v1'
& '$ServicePython' '$CanaryScript' --project-root '$ProjectRoot' --expect-base-prefix '$MachinePython' --expected-user '$AccountName' --result-path '$ResultPath' --tdx-root '$TdxRoot' --probe-model 'gpt-5.6-sol' --probe-external-auth --probe-smtp-login --allow-maintenance-lock --expect-tdx-readonly
exit `$LASTEXITCODE
"@
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $action = New-ScheduledTaskAction `
        -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand $encoded" `
        -WorkingDirectory $ProjectRoot
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1)
    Register-PasswordTask $ValidationTaskName $action $trigger (New-TaskSettings) $QualifiedUser $PlainPassword "Temporary VCP Hunter service-account canary"
    try {
        $startedAt = Get-Date
        Start-ScheduledTask -TaskName $ValidationTaskName
        $result = Wait-CanaryResult $ValidationTaskName $ResultPath $startedAt 480
        if (-not $result.ok) {
            throw "Service-account canary reported a failed check."
        }
    }
    finally {
        Remove-TaskIfPresent $ValidationTaskName
    }
}

function New-ReportAction([string]$Mode = "", [string]$CapturePath = "") {
    $modeSuffix = if ($Mode) { " $Mode" } else { "" }
    $maintenanceAllowance = if ($Mode -eq "preflight") { ' && set "VCP_PREFLIGHT_ALLOW_MAINTENANCE_LOCK=1"' } else { "" }
    $captureSuffix = if ($CapturePath) { ' > "{0}" 2>&1' -f $CapturePath } else { "" }
    $batchArguments = '/d /c chcp 65001 >nul && set "PYTHON_EXE={0}" && set "RUN_DAILY_HOLD_SECONDS=0"{1} && call "{2}"{3}{4}' -f $ServicePython, $maintenanceAllowance, $BatchPath, $modeSuffix, $captureSuffix
    return New-ScheduledTaskAction `
        -Execute "$env:SystemRoot\System32\cmd.exe" `
        -Argument $batchArguments `
        -WorkingDirectory $ProjectRoot
}

function Register-And-RunBatchPreflight(
    [string]$QualifiedUser,
    [string]$PlainPassword
) {
    if (Test-Path -LiteralPath $BatchPreflightLog) {
        Remove-Item -LiteralPath $BatchPreflightLog -Force
    }
    $action = New-ReportAction "preflight" $BatchPreflightLog
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1)
    Register-PasswordTask $ValidationTaskName $action $trigger (New-TaskSettings) $QualifiedUser $PlainPassword "Temporary exact-action preflight canary"
    try {
        $startedAt = Get-Date
        Start-ScheduledTask -TaskName $ValidationTaskName
        $exitCode = Wait-TaskResult $ValidationTaskName $startedAt 180
        if ($exitCode -ne 0) {
            throw "Exact-action preflight canary failed with exit code $exitCode. See $BatchPreflightLog."
        }
    }
    finally {
        Remove-TaskIfPresent $ValidationTaskName
    }
}

function Start-MaintenanceLock([string]$ReadyPath, [string]$ReleasePath) {
    foreach ($path in @($ReadyPath, $ReleasePath)) {
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Force
        }
    }
    $argumentList = @(
        (Quote-ProcessArgument $MaintenanceLockScript),
        "--project-root", (Quote-ProcessArgument $ProjectRoot),
        "--ready-file", (Quote-ProcessArgument $ReadyPath),
        "--release-file", (Quote-ProcessArgument $ReleasePath),
        "--timeout-seconds", "1200"
    ) -join " "
    $process = Start-Process `
        -FilePath $ServicePython `
        -ArgumentList $argumentList `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -PassThru
    $holderPid = 0
    try {
        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Test-Path -LiteralPath $ReadyPath)) {
            if ($process.HasExited) {
                throw "Maintenance lock holder exited with code $($process.ExitCode)."
            }
            if ((Get-Date) -ge $deadline) {
                throw "Timed out acquiring the maintenance lock."
            }
            Start-Sleep -Milliseconds 250
        }
        $readyPid = [int](Get-Content -LiteralPath $ReadyPath -Raw -Encoding ASCII)
        $holderPid = $readyPid
        $lockInfo = Get-Content -LiteralPath $RunLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $holder = Get-CimInstance Win32_Process -Filter "ProcessId=$readyPid" -ErrorAction SilentlyContinue
        $machinePythonExe = Join-Path $MachinePython "python.exe"
        if ($process.HasExited -or -not $holder -or -not (Test-SamePath $holder.ExecutablePath $machinePythonExe) -or $lockInfo.pid -ne $readyPid -or $lockInfo.command -ne "maintenance") {
            throw "Maintenance lock ownership verification failed."
        }
        Start-Sleep -Milliseconds 250
        $lockInfo = Get-Content -LiteralPath $RunLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $holder = Get-Process -Id $readyPid -ErrorAction SilentlyContinue
        if ($process.HasExited -or -not $holder -or $lockInfo.pid -ne $readyPid -or $lockInfo.command -ne "maintenance") {
            throw "Maintenance lock holder did not remain healthy after readiness."
        }
        return [pscustomobject]@{
            Launcher = $process
            HolderPid = $readyPid
        }
    }
    catch {
        $failure = $_
        try {
            Stop-MaintenanceLock ([pscustomobject]@{ Launcher = $process; HolderPid = $holderPid }) $ReadyPath $ReleasePath
        }
        catch {
            Write-Warning "Maintenance lock startup cleanup failed: $($_.Exception.Message)"
        }
        throw $failure
    }
}

function Remove-MaintenanceLockFileIfOwned([int]$HolderPid) {
    if ($HolderPid -le 0 -or -not (Test-Path -LiteralPath $RunLockPath)) {
        return
    }
    try {
        $lockInfo = Get-Content -LiteralPath $RunLockPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($lockInfo.pid -eq $HolderPid -and $lockInfo.command -eq "maintenance") {
            Remove-Item -LiteralPath $RunLockPath -Force
        }
    }
    catch {
        return
    }
}

function Stop-MaintenanceLock($Handle, [string]$ReadyPath, [string]$ReleasePath) {
    if (-not $Handle) {
        return
    }
    $launcher = $Handle.Launcher
    $holderPid = [int]$Handle.HolderPid
    try {
        if (-not $launcher.HasExited) {
            [IO.File]::WriteAllText($ReleasePath, "release", [Text.Encoding]::ASCII)
            if (-not $launcher.WaitForExit(30000)) {
                if ($holderPid -gt 0) {
                    Stop-Process -Id $holderPid -Force -ErrorAction SilentlyContinue
                }
                Stop-Process -Id $launcher.Id -Force -ErrorAction SilentlyContinue
                if ($launcher.WaitForExit(10000)) {
                    Remove-MaintenanceLockFileIfOwned $holderPid
                }
                throw "Maintenance lock holder did not stop cleanly."
            }
        }
        if ($launcher.ExitCode -ne 0) {
            Remove-MaintenanceLockFileIfOwned $holderPid
            throw "Maintenance lock holder exited with code $($launcher.ExitCode)."
        }
    }
    finally {
        foreach ($path in @($ReadyPath, $ReleasePath)) {
            if (Test-Path -LiteralPath $path) {
                Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

function Remove-TaskIfPresent([string]$Name) {
    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }
}

function Install-PrivateEgressBlock([hashtable]$State) {
    $existing = @($FirewallRuleNames | Where-Object {
        Get-NetFirewallRule -DisplayName $_ -ErrorAction SilentlyContinue
    })
    if ($existing) {
        throw "Managed firewall rule already exists: $($existing -join ', ')"
    }
    New-NetFirewallRule `
        -DisplayName $FirewallRuleNames[0] `
        -Group "VCP Hunter" `
        -Direction Outbound `
        -Action Block `
        -Program $ServicePython `
        -Profile Any `
        -RemoteAddress @("10.0.0.0/8", "100.64.0.0/10", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "224.0.0.0/4", "240.0.0.0/4") | Out-Null
    $State.FirewallRulesCreated += $FirewallRuleNames[0]
    New-NetFirewallRule `
        -DisplayName $FirewallRuleNames[1] `
        -Group "VCP Hunter" `
        -Direction Outbound `
        -Action Block `
        -Program $ServicePython `
        -Profile Any `
        -RemoteAddress @("fc00::/7", "fe80::/10", "ff00::/8") | Out-Null
    $State.FirewallRulesCreated += $FirewallRuleNames[1]
}

function Remove-MigrationFirewallRules([string[]]$Names) {
    $errors = [Collections.Generic.List[string]]::new()
    foreach ($name in $Names) {
        try {
            $rule = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
            if ($rule) {
                $rule | Remove-NetFirewallRule
            }
        }
        catch {
            $errors.Add("${name}: $($_.Exception.Message)") | Out-Null
        }
    }
    if ($errors.Count -gt 0) {
        throw "Firewall rollback errors: $($errors -join '; ')"
    }
}

function Remove-ManagedProxyDirectory {
    if (-not (Test-Path -LiteralPath $ProxyTargetRoot)) {
        return
    }
    $resolved = [IO.Path]::GetFullPath($ProxyTargetRoot)
    $allowedRoot = [IO.Path]::GetFullPath($ManagedProxyRoot)
    if (-not $resolved.StartsWith($allowedRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove unexpected proxy target: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Get-ManagedRunnerTasks {
    $tasks = foreach ($name in @($TaskName, $ValidationTaskName, $ProxyCanaryTaskName, $ProxyTaskName)) {
        $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($task -and $task.Principal.UserId -like "*$AccountName") {
            $task
        }
    }
    return @($tasks)
}

function Restore-MigrationState(
    [string]$OriginalTaskXml,
    $AclSnapshot,
    [hashtable]$State,
    [bool]$OriginalTaskWasEnabled
) {
    $errors = [Collections.Generic.List[string]]::new()

    try {
        if ($State.ProxyCanaryTaskCreated) {
            Remove-TaskIfPresent $ProxyCanaryTaskName
            Stop-ExpectedListener 8318 $ProxyTargetExe
            $State.ProxyCanaryTaskCreated = $false
        }
    }
    catch {
        $errors.Add("proxy canary task: $($_.Exception.Message)") | Out-Null
    }

    try {
        if ($State.ProxyTaskCreated) {
            $managedProxyOwnedPort = $false
            $listenerProcess = Get-ListenerProcess 8317
            if ($listenerProcess) {
                $managedProxyOwnedPort = Test-SamePath $listenerProcess.ExecutablePath $ProxyTargetExe
            }
            Remove-TaskIfPresent $ProxyTaskName
            if ($managedProxyOwnedPort) {
                Wait-PortClosed 8317
            }
            $State.ProxyTaskCreated = $false
        }
    }
    catch {
        $errors.Add("production proxy task: $($_.Exception.Message)") | Out-Null
    }

    try {
        Remove-TaskIfPresent $ValidationTaskName
    }
    catch {
        $errors.Add("validation task: $($_.Exception.Message)") | Out-Null
    }

    try {
        if ($State.ReportTaskReplaced) {
            Remove-TaskIfPresent $TaskName
            Register-ScheduledTask -TaskName $TaskName -Xml $OriginalTaskXml -Force | Out-Null
            if ($OriginalTaskWasEnabled) {
                Disable-ScheduledTask -TaskName $TaskName | Out-Null
                $State.OriginalTaskNeedsEnable = $true
            }
            $State.ReportTaskReplaced = $false
        }
        elseif ($State.OriginalTaskDisabled -and $OriginalTaskWasEnabled) {
            $State.OriginalTaskNeedsEnable = $true
        }
    }
    catch {
        $errors.Add("report task: $($_.Exception.Message)") | Out-Null
    }

    try {
        if ($State.FirewallRulesCreated.Count -gt 0) {
            Remove-MigrationFirewallRules $State.FirewallRulesCreated
            $State.FirewallRulesCreated = @()
        }
    }
    catch {
        $errors.Add("firewall rules: $($_.Exception.Message)") | Out-Null
    }

    try {
        if ($State.AclModified) {
            Restore-AclSnapshot $AclSnapshot
            $State.AclModified = $false
        }
    }
    catch {
        $errors.Add("ACL snapshot: $($_.Exception.Message)") | Out-Null
    }

    try {
        if ($State.ProxyDirectoryCreated) {
            $remainingProxyTasks = @(
                foreach ($name in @($ProxyCanaryTaskName, $ProxyTaskName)) {
                    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
                        $name
                    }
                }
            )
            if ($remainingProxyTasks.Count -gt 0) {
                throw "refusing to remove proxy files while managed proxy tasks remain"
            }
            Remove-ManagedProxyDirectory
            $State.ProxyDirectoryCreated = $false
            if ($State.ProxyParentCreated) {
                $parent = Split-Path -Parent $ProxyTargetRoot
                if ((Test-Path -LiteralPath $parent) -and -not (Get-ChildItem -LiteralPath $parent -Force)) {
                    Remove-Item -LiteralPath $parent -Force
                }
            }
        }
    }
    catch {
        $errors.Add("proxy directory: $($_.Exception.Message)") | Out-Null
    }

    try {
        if ($State.AccountCreated -and (Get-LocalUser -Name $AccountName -ErrorAction SilentlyContinue)) {
            $runnerTasks = @(Get-ManagedRunnerTasks)
            if ($runnerTasks.Count -gt 0) {
                throw "refusing to remove the runner account while managed tasks still use it"
            }
            if ($State.BatchLogonGranted) {
                $runner = Get-LocalUser -Name $AccountName -ErrorAction Stop
                $runnerSid = [Security.Principal.SecurityIdentifier]::new($runner.Sid.Value)
                Set-BatchLogonRight $runnerSid $false
                $State.BatchLogonGranted = $false
            }
            Remove-LocalUser -Name $AccountName
            $State.AccountCreated = $false
        }
    }
    catch {
        $errors.Add("local account: $($_.Exception.Message)") | Out-Null
    }

    try {
        if ($State.OriginalProxyStopped -and -not (Get-NetTCPConnection -LocalPort 8317 -State Listen -ErrorAction SilentlyContinue)) {
            $proxyArguments = "--config " + (Quote-ProcessArgument $ProxySourceConfig)
            Start-Process `
                -FilePath $ProxySourceExe `
                -ArgumentList $proxyArguments `
                -WorkingDirectory $ProxySourceRoot `
                -WindowStyle Hidden | Out-Null
            Wait-TcpListener 8317 | Out-Null
            Assert-ListenerOwnedBy 8317 $ProxySourceExe
        }
    }
    catch {
        $errors.Add("source proxy: $($_.Exception.Message)") | Out-Null
    }

    if ($errors.Count -gt 0) {
        throw "Rollback resource errors: $($errors -join '; ')"
    }
}

$readiness = Get-ReadinessReport
if (-not $Apply) {
    [ordered]@{
        mode = "dry-run"
        ready = $readiness.ready
        checks = $readiness.checks
        target_principal = "$env:COMPUTERNAME\$AccountName"
        service_python = $ServicePython
        proxy_source = $ProxySourceRoot
        proxy_target = $ProxyTargetRoot
        planned_changes = @(
            "disable the existing trigger and hold the pipeline maintenance lock",
            "create a dedicated standard local account with a random Task Scheduler password",
            "grant source read/execute, output modify, and a runner-specific D:\HT deny-write ACE",
            "copy CLIProxyAPI only after its ProgramData directory has a private ACL",
            "grant proxy app/config read-execute and only auths modify access",
            "run real inference, external API, SMTP login, TDX write-denial, and strict preflight canaries",
            "switch port 8317 only after the isolated port-8318 canary passes",
            "exercise the final cmd.exe action with preflight before replacing the report task",
            "roll back only resources created or changed by this invocation"
        )
    } | ConvertTo-Json -Depth 6
    if (-not $readiness.ready) {
        exit 2
    }
    return
}

if (-not $readiness.ready) {
    $readiness | ConvertTo-Json -Depth 6
    exit 2
}

if (-not (Test-IsAdministrator)) {
    Invoke-ElevatedSelf
    return
}

$readiness = Get-ReadinessReport
if (-not $readiness.ready) {
    $readiness | ConvertTo-Json -Depth 6
    exit 2
}

$existingTask = Get-ScheduledTask -TaskName $TaskName
$originalTaskWasEnabled = $existingTask.State -ne "Disabled"
$originalTaskXml = Export-ScheduledTask -TaskName $TaskName
$aclPaths = @($ProjectRoot, $BatchPath, $AppRoot, $EnvPath, $ServiceVenv, $CanaryScript, $OutputRoot, $TdxRoot)
$aclSnapshot = Get-AclSnapshot $aclPaths
$state = @{
    AccountCreated = $false
    AclModified = $false
    BatchLogonGranted = $false
    FirewallRulesCreated = @()
    MigrationResultWritten = $false
    OriginalProxyStopped = $false
    OriginalTaskDisabled = $false
    OriginalTaskNeedsEnable = $false
    ProxyCanaryTaskCreated = $false
    ProxyDirectoryCreated = $false
    ProxyParentCreated = $false
    ProxyTaskCreated = $false
    ReportTaskReplaced = $false
}
$plainPassword = $null
$securePassword = $null
$maintenanceProcess = $null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupDir = Join-Path $BackupRoot "daily-report-task-migration-$stamp"
$maintenanceReady = Join-Path $backupDir "maintenance.ready"
$maintenanceRelease = Join-Path $backupDir "maintenance.release"
$migrationResultPath = Join-Path $backupDir "migration-result.json"

try {
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    Set-PrivateDirectoryAcl $backupDir
    [IO.File]::WriteAllText((Join-Path $backupDir "original-task.xml"), $originalTaskXml, [Text.Encoding]::Unicode)
    [IO.File]::WriteAllText(
        (Join-Path $backupDir "acl-snapshot.json"),
        ($aclSnapshot | ConvertTo-Json -Depth 4),
        [Text.UTF8Encoding]::new($false)
    )

    if ($originalTaskWasEnabled) {
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        $state.OriginalTaskDisabled = $true
    }
    $maintenanceProcess = Start-MaintenanceLock $maintenanceReady $maintenanceRelease

    $plainPassword = New-StrongPassword
    $securePassword = ConvertTo-SecureString $plainPassword -AsPlainText -Force
    $runner = New-LocalUser `
        -Name $AccountName `
        -Password $securePassword `
        -AccountNeverExpires `
        -PasswordNeverExpires `
        -UserMayNotChangePassword `
        -Description "VCP Hunter scheduled-task runner"
    $state.AccountCreated = $true
    & "$env:SystemRoot\System32\net.exe" user $AccountName /passwordreq:yes 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to require a password for the dedicated runner account."
    }
    $runner = Get-LocalUser -Name $AccountName -ErrorAction Stop
    if (-not $runner.PasswordRequired) {
        throw "The dedicated runner account still has PASSWD_NOTREQD set."
    }
    $runnerSid = [Security.Principal.SecurityIdentifier]::new($runner.Sid.Value)
    $qualifiedUser = "$env:COMPUTERNAME\$AccountName"
    Set-BatchLogonRight $runnerSid $true
    $state.BatchLogonGranted = $true

    $readExecute = [Security.AccessControl.FileSystemRights]"ReadAndExecute, Synchronize"
    $modify = [Security.AccessControl.FileSystemRights]"Modify, Synchronize"
    $inherit = [Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
    Grant-PathAccess $ProjectRoot $runnerSid $readExecute
    $state.AclModified = $true
    Grant-PathAccess $BatchPath $runnerSid $readExecute
    Grant-PathAccess $AppRoot $runnerSid $readExecute $inherit
    Grant-PathAccess $EnvPath $runnerSid $readExecute
    Grant-PathAccess $ServiceVenv $runnerSid $readExecute $inherit
    Grant-PathAccess $CanaryScript $runnerSid $readExecute
    Grant-PathAccess $OutputRoot $runnerSid $modify $inherit
    Set-TdxReadOnlyAccess $TdxRoot $runnerSid
    Install-PrivateEgressBlock $state

    $proxyParent = Split-Path -Parent $ProxyTargetRoot
    if (-not (Test-Path -LiteralPath $proxyParent)) {
        New-Item -ItemType Directory -Path $proxyParent -Force | Out-Null
        $state.ProxyParentCreated = $true
    }
    New-Item -ItemType Directory -Path $ProxyTargetRoot | Out-Null
    $state.ProxyDirectoryCreated = $true
    Set-PrivateDirectoryAcl $ProxyTargetRoot
    Get-ChildItem -LiteralPath $ProxySourceRoot -Force | Copy-Item -Destination $ProxyTargetRoot -Recurse -Force
    Set-PrivateDirectoryAcl $ProxyTargetRoot $runnerSid
    Grant-PathAccess (Join-Path $ProxyTargetRoot "auths") $runnerSid $modify $inherit
    Set-ProxyConfig $ProxySourceConfig $ProxyTargetConfig 8317
    Set-ProxyConfig $ProxySourceConfig $CanaryProxyConfig 8318

    $proxySettings = New-TaskSettings
    $proxyTrigger = New-ScheduledTaskTrigger -AtStartup
    $proxyCanaryAction = New-ScheduledTaskAction `
        -Execute $ProxyTargetExe `
        -Argument "--config `"$CanaryProxyConfig`"" `
        -WorkingDirectory $ProxyTargetRoot
    Register-PasswordTask $ProxyCanaryTaskName $proxyCanaryAction (New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1)) $proxySettings $qualifiedUser $plainPassword "Temporary CLIProxyAPI service-account canary"
    $state.ProxyCanaryTaskCreated = $true
    $proxyCanaryStartedAt = Get-Date
    Start-ScheduledTask -TaskName $ProxyCanaryTaskName
    Wait-TaskTcpListener 8318 $ProxyCanaryTaskName $proxyCanaryStartedAt | Out-Null
    Assert-ListenerOwnedBy 8318 $ProxyTargetExe
    $canaryResult = Join-Path $OutputRoot "_system\scheduled-task-account-canary-8318.json"
    Register-And-RunCanary $qualifiedUser $plainPassword $canaryResult 8318
    Remove-TaskIfPresent $ProxyCanaryTaskName
    Stop-ExpectedListener 8318 $ProxyTargetExe
    $state.ProxyCanaryTaskCreated = $false
    Remove-Item -LiteralPath $CanaryProxyConfig -Force

    $proxyAction = New-ScheduledTaskAction `
        -Execute $ProxyTargetExe `
        -Argument "--config `"$ProxyTargetConfig`"" `
        -WorkingDirectory $ProxyTargetRoot
    Register-PasswordTask $ProxyTaskName $proxyAction $proxyTrigger $proxySettings $qualifiedUser $plainPassword "Persistent local model proxy for VCP Hunter"
    $state.ProxyTaskCreated = $true

    $oldProxy = Get-ListenerProcess 8317
    if (-not $oldProxy -or -not (Test-SamePath $oldProxy.ExecutablePath $ProxySourceExe)) {
        throw "The source proxy no longer exclusively owns port 8317."
    }
    Stop-Process -Id $oldProxy.ProcessId -Force
    $state.OriginalProxyStopped = $true
    Wait-PortClosed 8317
    $proxyStartedAt = Get-Date
    Start-ScheduledTask -TaskName $ProxyTaskName
    Wait-TaskTcpListener 8317 $ProxyTaskName $proxyStartedAt | Out-Null
    Assert-ListenerOwnedBy 8317 $ProxyTargetExe
    $liveResult = Join-Path $OutputRoot "_system\scheduled-task-account-canary-8317.json"
    Register-And-RunCanary $qualifiedUser $plainPassword $liveResult 8317

    Register-And-RunBatchPreflight $qualifiedUser $plainPassword

    $reportAction = New-ReportAction
    Register-PasswordTask `
        $TaskName `
        $reportAction `
        $existingTask.Triggers `
        $existingTask.Settings `
        $qualifiedUser `
        $plainPassword `
        "VCP Hunter daily report under a dedicated least-privilege account" `
        -ReplaceExisting
    $state.ReportTaskReplaced = $true

    $reportTask = Get-ScheduledTask -TaskName $TaskName
    $proxyTask = Get-ScheduledTask -TaskName $ProxyTaskName
    $runnerVerification = Get-LocalUser -Name $AccountName -ErrorAction Stop
    $reportLogonType = [string]$reportTask.Principal.LogonType
    if ($reportTask.Principal.UserId -notlike "*$AccountName" -or $reportTask.Principal.RunLevel -ne "Limited" -or $reportLogonType -ne "Password") {
        throw "Report task principal verification failed."
    }
    if ($proxyTask.Principal.UserId -notlike "*$AccountName" -or $proxyTask.State -ne "Running") {
        throw "Proxy task verification failed."
    }
    if (-not $runnerVerification.Enabled -or -not $runnerVerification.PasswordRequired) {
        throw "Runner account password policy verification failed."
    }
    Assert-ListenerOwnedBy 8317 $ProxyTargetExe

    $migrationResult = [ordered]@{
        completed_at = [DateTimeOffset]::Now.ToString("o")
        task = $TaskName
        proxy_task = $ProxyTaskName
        principal = $reportTask.Principal.UserId
        logon_type = $reportLogonType
        run_level = [string]$reportTask.Principal.RunLevel
        service_python = $ServicePython
        proxy_state = [string]$proxyTask.State
        private_egress_firewall_rules = $FirewallRuleNames
        canary_8318 = $canaryResult
        canary_8317 = $liveResult
        exact_action_preflight = $true
        exact_action_preflight_log = $BatchPreflightLog
        batch_logon_right_granted = $true
        tdx_write_denied_for_runner = $true
        backup_dir = $backupDir
        password_persisted_outside_task_scheduler = $false
        password_required = $true
        credentials_rotation_check_passed = [bool](
            $readiness.checks |
                Where-Object { $_.name -eq "current_credentials_rotated" } |
                Select-Object -First 1 -ExpandProperty ok
        )
        unrotated_credentials_risk_accepted = [bool]$AcceptUnrotatedCredentialsRisk
    }
    [IO.File]::WriteAllText(
        $migrationResultPath,
        ($migrationResult | ConvertTo-Json -Depth 4),
        [Text.UTF8Encoding]::new($false)
    )
    $state.MigrationResultWritten = $true
    Stop-MaintenanceLock $maintenanceProcess $maintenanceReady $maintenanceRelease
    $maintenanceProcess = $null
    $migrationResult | ConvertTo-Json -Depth 4
}
catch {
    $failure = $_
    $rollbackErrors = [Collections.Generic.List[string]]::new()
    if ($state.MigrationResultWritten) {
        try {
            Remove-Item -LiteralPath $migrationResultPath -Force
            $state.MigrationResultWritten = $false
        }
        catch {
            $rollbackErrors.Add($_.Exception.Message) | Out-Null
        }
    }
    try {
        Restore-MigrationState $originalTaskXml $aclSnapshot $state $originalTaskWasEnabled
    }
    catch {
        $rollbackErrors.Add($_.Exception.Message) | Out-Null
    }
    try {
        Stop-MaintenanceLock $maintenanceProcess $maintenanceReady $maintenanceRelease
        $maintenanceProcess = $null
    }
    catch {
        $rollbackErrors.Add($_.Exception.Message) | Out-Null
    }
    if ($state.OriginalTaskNeedsEnable) {
        try {
            Enable-ScheduledTask -TaskName $TaskName | Out-Null
            $state.OriginalTaskNeedsEnable = $false
        }
        catch {
            $rollbackErrors.Add($_.Exception.Message) | Out-Null
        }
    }
    if (Test-Path -LiteralPath $backupDir) {
        try {
            $safeFailureMessage = [string]$failure.Exception.Message
            if ($plainPassword) {
                $safeFailureMessage = $safeFailureMessage.Replace($plainPassword, "<redacted>")
            }
            $safeFailureMessage = [regex]::Replace(
                $safeFailureMessage,
                '(?i)(Bearer\s+|sk-|AIza)[A-Za-z0-9._-]{8,}',
                '<redacted>'
            )
            $failureReport = [ordered]@{
                failed_at = [DateTimeOffset]::Now.ToString("o")
                failure_type = $failure.Exception.GetType().Name
                failure_message = $safeFailureMessage
                rollback_errors = @($rollbackErrors)
                unrotated_credentials_risk_accepted = [bool]$AcceptUnrotatedCredentialsRisk
                secrets_included = $false
            }
            [IO.File]::WriteAllText(
                (Join-Path $backupDir "migration-failure.json"),
                ($failureReport | ConvertTo-Json -Depth 4),
                [Text.UTF8Encoding]::new($false)
            )
        }
        catch {
            $rollbackErrors.Add("failure report: $($_.Exception.Message)") | Out-Null
        }
    }
    $rollbackDetail = if ($rollbackErrors.Count -eq 0) { "rollback completed" } else { "rollback errors: $($rollbackErrors -join '; ')" }
    throw "Migration failed: $($failure.Exception.Message); $rollbackDetail"
}
finally {
    if ($maintenanceProcess) {
        try {
            Stop-MaintenanceLock $maintenanceProcess $maintenanceReady $maintenanceRelease
        }
        catch {
            Write-Warning "Maintenance lock cleanup failed: $($_.Exception.Message)"
        }
    }
    if ($securePassword) {
        $securePassword.Dispose()
    }
    $plainPassword = $null
}
