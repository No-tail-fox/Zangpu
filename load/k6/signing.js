const HMAC_ALGORITHM = "ZANGPU-HMAC-SHA256";
const SIGNATURE_VERSION = "1";

function requireMatch(value, pattern, name) {
  if (typeof value !== "string" || !pattern.test(value)) {
    throw new Error(`${name} is invalid.`);
  }
}

export function createCanonicalRequest({ method, path, bodyHash, keyId, timestamp, nonce, requestId }) {
  requireMatch(method, /^[A-Za-z]+$/, "method");
  requireMatch(path, /^\/(?:[A-Za-z0-9._~-]+\/)*[A-Za-z0-9._~-]+$/, "path");
  requireMatch(bodyHash, /^[0-9a-f]{64}$/, "body hash");
  requireMatch(keyId, /^zpk_[A-Za-z0-9_-]{4,76}$/, "key ID");
  requireMatch(timestamp, /^(?:0|[1-9][0-9]{0,19})$/, "timestamp");
  requireMatch(nonce, /^[A-Za-z0-9_-]{16,128}$/, "nonce");
  requireMatch(requestId, /^[A-Za-z0-9_-]{16,64}$/, "request ID");
  return [
    HMAC_ALGORITHM,
    SIGNATURE_VERSION,
    method.toUpperCase(),
    path,
    "",
    bodyHash,
    keyId,
    timestamp,
    nonce,
    requestId,
  ].join("\n");
}

export function createSignedHeaders({
  method,
  path,
  body,
  keyId,
  secret,
  timestamp,
  nonce,
  requestId,
  sha256Hex,
  hmacSha256Hex,
}) {
  if (typeof body !== "string" || typeof secret !== "string" || secret.length < 1 || secret.length > 4096) {
    throw new Error("signing input is invalid.");
  }
  if (typeof sha256Hex !== "function" || typeof hmacSha256Hex !== "function") {
    throw new Error("signing primitives are invalid.");
  }
  const canonical = createCanonicalRequest({
    method,
    path,
    bodyHash: sha256Hex(body),
    keyId,
    timestamp,
    nonce,
    requestId,
  });
  const signature = hmacSha256Hex(secret, canonical);
  requireMatch(signature, /^[0-9a-f]{64}$/, "signature");
  return {
    "X-Zangpu-Key": keyId,
    "X-Zangpu-Timestamp": timestamp,
    "X-Zangpu-Nonce": nonce,
    "X-Zangpu-Request-Id": requestId,
    "X-Zangpu-Signature-Version": SIGNATURE_VERSION,
    "X-Zangpu-Signature": signature,
  };
}
