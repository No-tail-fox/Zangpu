[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet("smoke", "steady", "burst", "concurrency")]
    [string]$Profile = "smoke",

    [Parameter()]
    [ValidateSet("models", "usage", "chat")]
    [string]$Target = "models",

    [Parameter()]
    [ValidateRange(1, 300000)]
    [int]$P95Milliseconds,

    [Parameter()]
    [ValidateRange(0.0, 0.5)]
    [double]$MaxFailureRate = 0.01,

    [Parameter()]
    [switch]$ConfirmChatSpend,

    [Parameter()]
    [ValidateRange(1, 10000)]
    [int]$ExpectedConcurrencyLimit,

    [Parameter()]
    [ValidateRange(2, 10000)]
    [int]$ConcurrencyAttempts,

    [Parameter()]
    [string]$OutputDirectory,

    [Parameter()]
    [string]$K6Exe = $env:K6_EXE
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

foreach ($name in @("ZANGPU_API_BASE_URL", "ZANGPU_API_KEY_ID", "ZANGPU_API_SECRET")) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name, "Process"))) {
        throw "$name is required in the process environment."
    }
}
if ($Target -eq "chat" -and -not $ConfirmChatSpend) {
    throw "Chat load can spend credit and requires -ConfirmChatSpend."
}
if ($Target -eq "chat" -and [string]::IsNullOrWhiteSpace($env:ZANGPU_LOAD_MODEL)) {
    throw "ZANGPU_LOAD_MODEL is required for chat load."
}
if ($Profile -eq "concurrency" -and $Target -ne "chat") {
    throw "Concurrency profile requires -Target chat."
}
if ($Profile -eq "concurrency" -and -not $PSBoundParameters.ContainsKey("ExpectedConcurrencyLimit")) {
    throw "Concurrency profile requires -ExpectedConcurrencyLimit."
}
if ($Profile -eq "concurrency" -and -not $PSBoundParameters.ContainsKey("ConcurrencyAttempts")) {
    throw "Concurrency profile requires -ConcurrencyAttempts."
}
if ($Profile -eq "concurrency" -and $ConcurrencyAttempts -le $ExpectedConcurrencyLimit) {
    throw "ConcurrencyAttempts must exceed ExpectedConcurrencyLimit."
}

if ([string]::IsNullOrWhiteSpace($K6Exe)) {
    $command = Get-Command k6 -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "k6 was not found. Set K6_EXE to a verified k6 executable."
    }
    $K6Exe = $command.Source
}
$resolvedK6 = (Resolve-Path -LiteralPath $K6Exe).Path
$scriptPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "signed-api.js")).Path
$repositoryRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repositoryRoot ".tmp\k6-results"
}
$resultDirectory = [IO.Path]::GetFullPath($OutputDirectory)
[IO.Directory]::CreateDirectory($resultDirectory) | Out-Null
$timestamp = [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$summaryJson = Join-Path $resultDirectory "$timestamp-$Target-$Profile.json"
$summaryText = Join-Path $resultDirectory "$timestamp-$Target-$Profile.txt"

$overrides = @{
    ZANGPU_LOAD_PROFILE = $Profile
    ZANGPU_LOAD_TARGET = $Target
    ZANGPU_LOAD_MAX_FAILURE_RATE = $MaxFailureRate.ToString([Globalization.CultureInfo]::InvariantCulture)
    ZANGPU_LOAD_SUMMARY_JSON = $summaryJson
    ZANGPU_LOAD_SUMMARY_TEXT = $summaryText
}
if ($PSBoundParameters.ContainsKey("P95Milliseconds")) {
    $overrides.ZANGPU_LOAD_P95_MS = $P95Milliseconds.ToString([Globalization.CultureInfo]::InvariantCulture)
}
if ($ConfirmChatSpend) {
    $overrides.ZANGPU_LOAD_CONFIRM_CHAT = "YES"
}
if ($Profile -eq "concurrency") {
    $overrides.ZANGPU_LOAD_EXPECTED_CONCURRENCY_LIMIT = $ExpectedConcurrencyLimit.ToString(
        [Globalization.CultureInfo]::InvariantCulture
    )
    $overrides.ZANGPU_LOAD_CONCURRENCY_ATTEMPTS = $ConcurrencyAttempts.ToString(
        [Globalization.CultureInfo]::InvariantCulture
    )
}

$previous = @{}
try {
    foreach ($entry in $overrides.GetEnumerator()) {
        $previous[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key, "Process")
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }
    & $resolvedK6 run --quiet $scriptPath
    $exitCode = $LASTEXITCODE
}
finally {
    foreach ($entry in $previous.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, "Process")
    }
}

if ($exitCode -ne 0) {
    exit $exitCode
}
Write-Output "k6 summary JSON: $summaryJson"
Write-Output "k6 summary text: $summaryText"
