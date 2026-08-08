[CmdletBinding()]
param(
    [Parameter()]
    [string]$BaseUrl = $env:ZANGPU_API_BASE_URL,

    [Parameter()]
    [string]$KeyId = $env:ZANGPU_API_KEY_ID,

    [Parameter(Mandatory = $true)]
    [ValidateSet(
        "/api/v1/external/models",
        "/api/v1/external/usage",
        "/api/v1/external/chat/completions"
    )]
    [string]$Path,

    [Parameter()]
    [ValidateSet("GET", "POST")]
    [string]$Method = "GET",

    [Parameter()]
    [string]$BodyFile,

    [Parameter()]
    [string]$RequestId,

    [Parameter()]
    [switch]$IncludeResponseHeaders
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$secretValue = $env:ZANGPU_API_SECRET
if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
    throw "BaseUrl or ZANGPU_API_BASE_URL is required."
}
if ($KeyId -notmatch '^zpk_[A-Za-z0-9_-]{4,76}$') {
    throw "KeyId or ZANGPU_API_KEY_ID is invalid."
}
if ([string]::IsNullOrEmpty($secretValue)) {
    throw "ZANGPU_API_SECRET is required."
}

$origin = $null
if (-not [Uri]::TryCreate($BaseUrl, [UriKind]::Absolute, [ref]$origin)) {
    throw "BaseUrl must be an absolute origin."
}
$isLoopback = $origin.Host -in @("127.0.0.1", "localhost", "::1")
if (
    $origin.Scheme -notin @("http", "https") -or
    -not [string]::IsNullOrEmpty($origin.UserInfo) -or
    $origin.AbsolutePath -ne "/" -or
    -not [string]::IsNullOrEmpty($origin.Query) -or
    -not [string]::IsNullOrEmpty($origin.Fragment) -or
    ($origin.Scheme -eq "http" -and -not $isLoopback)
) {
    throw "BaseUrl must be an HTTPS origin or a loopback HTTP origin."
}
$originText = $origin.GetLeftPart([UriPartial]::Authority)

$methodValue = $Method.ToUpperInvariant()
if ($Path -eq "/api/v1/external/chat/completions" -and $methodValue -ne "POST") {
    throw "The chat route requires POST."
}
if ($Path -ne "/api/v1/external/chat/completions" -and $methodValue -ne "GET") {
    throw "Metadata routes require GET."
}

$bodyBytes = [byte[]]::new(0)
$resolvedBodyPath = $null
if ($methodValue -eq "POST") {
    if ([string]::IsNullOrWhiteSpace($BodyFile)) {
        throw "BodyFile is required for POST."
    }
    $resolvedBodyPath = (Resolve-Path -LiteralPath $BodyFile).Path
    $bodyBytes = [IO.File]::ReadAllBytes($resolvedBodyPath)
    if ($bodyBytes.Length -eq 0 -or $bodyBytes.Length -gt 1MB) {
        throw "BodyFile must contain between 1 byte and 1 MiB."
    }
}
elseif (-not [string]::IsNullOrWhiteSpace($BodyFile)) {
    throw "GET requests must have an empty body."
}

if ([string]::IsNullOrWhiteSpace($RequestId)) {
    $RequestId = "req_" + [Guid]::NewGuid().ToString("N")
}
if ($RequestId -notmatch '^[A-Za-z0-9_-]{16,64}$') {
    throw "RequestId is invalid."
}

$timestamp = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds().ToString()
$nonce = "nonce_" + [Guid]::NewGuid().ToString("N")
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $bodyHashBytes = $sha256.ComputeHash($bodyBytes)
}
finally {
    $sha256.Dispose()
}
$bodyHash = [Convert]::ToHexString($bodyHashBytes).ToLowerInvariant()
$canonical = @(
    "ZANGPU-HMAC-SHA256",
    "1",
    $methodValue,
    $Path,
    "",
    $bodyHash,
    $KeyId,
    $timestamp,
    $nonce,
    $RequestId
) -join "`n"

$hmac = [Security.Cryptography.HMACSHA256]::new([Text.Encoding]::UTF8.GetBytes($secretValue))
try {
    $signatureBytes = $hmac.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical))
}
finally {
    $hmac.Dispose()
}
$signature = [Convert]::ToHexString($signatureBytes).ToLowerInvariant()

$curlArguments = @(
    "--silent",
    "--show-error",
    "--no-buffer",
    "--proto",
    ("=" + $origin.Scheme),
    "--request",
    $methodValue,
    "--url",
    ($originText + $Path),
    "--header",
    ("X-Zangpu-Key: " + $KeyId),
    "--header",
    ("X-Zangpu-Timestamp: " + $timestamp),
    "--header",
    ("X-Zangpu-Nonce: " + $nonce),
    "--header",
    ("X-Zangpu-Request-Id: " + $RequestId),
    "--header",
    "X-Zangpu-Signature-Version: 1",
    "--header",
    ("X-Zangpu-Signature: " + $signature),
    "--header",
    "Accept: application/json, text/event-stream"
)
if ($methodValue -eq "POST") {
    $curlArguments += @(
        "--header",
        "Content-Type: application/json",
        "--data-binary",
        ("@" + $resolvedBodyPath)
    )
}
if ($IncludeResponseHeaders) {
    $curlArguments += "--include"
}

& curl.exe @curlArguments
$curlExitCode = $LASTEXITCODE
if ($curlExitCode -ne 0) {
    exit $curlExitCode
}
