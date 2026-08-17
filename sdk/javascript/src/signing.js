import { createHash, createHmac, randomUUID } from "node:crypto";

export const HMAC_ALGORITHM = "ZANGPU-HMAC-SHA256";
export const SIGNATURE_VERSION = "1";

const LOWER_HEX_SHA256_RE = /^[0-9a-f]{64}$/;
const KEY_ID_RE = /^zpk_[A-Za-z0-9_-]{4,76}$/;
const TIMESTAMP_RE = /^(?:0|[1-9][0-9]{0,19})$/;
const NONCE_RE = /^[A-Za-z0-9_-]{16,128}$/;
const REQUEST_ID_RE = /^[A-Za-z0-9_-]{16,64}$/;
const METHOD_RE = /^[A-Za-z]+$/;
const INVALID_PERCENT_RE = /%(?![0-9A-Fa-f]{2})/;
const PERCENT_ESCAPE_RE = /%[0-9A-Fa-f]{2}/;
const CONTROL_RE = /[\u0000-\u001f\u007f]/;

export class ZangpuSigningError extends TypeError {
  constructor(message) {
    super(message);
    this.name = "ZangpuSigningError";
  }
}

function decodeComponent(value, { rejectDoubleEncoding }) {
  if (INVALID_PERCENT_RE.test(value)) {
    throw new ZangpuSigningError("malformed percent encoding");
  }
  let decoded;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    throw new ZangpuSigningError("component is not valid UTF-8");
  }
  if (CONTROL_RE.test(decoded)) {
    throw new ZangpuSigningError("component contains a control character");
  }
  if (rejectDoubleEncoding && PERCENT_ESCAPE_RE.test(decoded)) {
    throw new ZangpuSigningError("double encoding is not allowed");
  }
  return decoded;
}

function rfc3986Encode(value) {
  try {
    return encodeURIComponent(value).replace(
      /[!'()*]/g,
      (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`,
    );
  } catch {
    throw new ZangpuSigningError("component is not valid UTF-8");
  }
}

export function canonicalizePath(rawPath) {
  if (
    typeof rawPath !== "string" ||
    !rawPath.startsWith("/") ||
    rawPath.includes("?") ||
    rawPath.includes("#")
  ) {
    throw new ZangpuSigningError(
      "path must be absolute and exclude query or fragment",
    );
  }
  return rawPath
    .split("/")
    .map((rawSegment) => {
      const segment = decodeComponent(rawSegment, {
        rejectDoubleEncoding: true,
      });
      if (segment === "." || segment === ".." || segment.includes("\\")) {
        throw new ZangpuSigningError("ambiguous path segment");
      }
      return rfc3986Encode(segment);
    })
    .join("/");
}

export function canonicalizeQuery(rawQuery) {
  if (typeof rawQuery !== "string") {
    throw new ZangpuSigningError("query must be text");
  }
  if (rawQuery === "") {
    return "";
  }
  const pairs = rawQuery.split("&").map((rawPair) => {
    const separator = rawPair.indexOf("=");
    const rawKey = separator === -1 ? rawPair : rawPair.slice(0, separator);
    const rawValue = separator === -1 ? "" : rawPair.slice(separator + 1);
    return [
      rfc3986Encode(decodeComponent(rawKey, { rejectDoubleEncoding: false })),
      rfc3986Encode(decodeComponent(rawValue, { rejectDoubleEncoding: false })),
    ];
  });
  pairs.sort(([leftKey, leftValue], [rightKey, rightValue]) => {
    if (leftKey !== rightKey) {
      return leftKey < rightKey ? -1 : 1;
    }
    if (leftValue === rightValue) {
      return 0;
    }
    return leftValue < rightValue ? -1 : 1;
  });
  return pairs.map(([key, value]) => `${key}=${value}`).join("&");
}

function validateFields({ keyId, timestamp, nonce, requestId }) {
  if (typeof keyId !== "string" || !KEY_ID_RE.test(keyId)) {
    throw new ZangpuSigningError("invalid key ID");
  }
  if (typeof timestamp !== "string" || !TIMESTAMP_RE.test(timestamp)) {
    throw new ZangpuSigningError("invalid timestamp");
  }
  if (typeof nonce !== "string" || !NONCE_RE.test(nonce)) {
    throw new ZangpuSigningError("invalid nonce");
  }
  if (typeof requestId !== "string" || !REQUEST_ID_RE.test(requestId)) {
    throw new ZangpuSigningError("invalid request ID");
  }
}

export function createCanonicalRequest({
  method,
  path,
  query = "",
  bodyHash,
  keyId,
  timestamp,
  nonce,
  requestId,
}) {
  if (typeof method !== "string" || !METHOD_RE.test(method)) {
    throw new ZangpuSigningError("invalid HTTP method");
  }
  if (typeof bodyHash !== "string" || !LOWER_HEX_SHA256_RE.test(bodyHash)) {
    throw new ZangpuSigningError("invalid body hash");
  }
  validateFields({ keyId, timestamp, nonce, requestId });
  return [
    HMAC_ALGORITHM,
    SIGNATURE_VERSION,
    method.toUpperCase(),
    canonicalizePath(path),
    canonicalizeQuery(query),
    bodyHash,
    keyId,
    timestamp,
    nonce,
    requestId,
  ].join("\n");
}

export function bodySha256Hex(body) {
  if (!(body instanceof Uint8Array)) {
    throw new ZangpuSigningError("body must be raw bytes");
  }
  return createHash("sha256").update(body).digest("hex");
}

export class SignedHeaders {
  #keyId;
  #nonce;
  #requestId;
  #signature;
  #timestamp;

  constructor({ keyId, timestamp, nonce, requestId, signature }) {
    this.#keyId = keyId;
    this.#timestamp = timestamp;
    this.#nonce = nonce;
    this.#requestId = requestId;
    this.#signature = signature;
    Object.freeze(this);
  }

  get keyId() {
    return this.#keyId;
  }

  get timestamp() {
    return this.#timestamp;
  }

  get nonce() {
    return this.#nonce;
  }

  get requestId() {
    return this.#requestId;
  }

  get signature() {
    return this.#signature;
  }

  get headers() {
    return Object.freeze({
      "x-zangpu-key": this.#keyId,
      "x-zangpu-timestamp": this.#timestamp,
      "x-zangpu-nonce": this.#nonce,
      "x-zangpu-request-id": this.#requestId,
      "x-zangpu-signature-version": SIGNATURE_VERSION,
      "x-zangpu-signature": this.#signature,
    });
  }

  toJSON() {
    return {
      keyId: this.#keyId,
      requestId: this.#requestId,
      nonce: "<redacted>",
      signature: "<redacted>",
    };
  }
}

export class ZangpuSigner {
  #clock;
  #keyId;
  #nonceFactory;
  #requestIdFactory;
  #secret;

  constructor({
    keyId,
    secret,
    clock = () => Date.now() / 1000,
    nonceFactory = () => `nonce_${randomUUID().replaceAll("-", "")}`,
    requestIdFactory = () => `req_${randomUUID().replaceAll("-", "")}`,
  }) {
    if (typeof keyId !== "string" || !KEY_ID_RE.test(keyId)) {
      throw new ZangpuSigningError("invalid key ID");
    }
    if (
      typeof secret !== "string" ||
      secret.length === 0 ||
      secret.length > 4096
    ) {
      throw new ZangpuSigningError("invalid signing material");
    }
    if (
      ![clock, nonceFactory, requestIdFactory].every(
        (value) => typeof value === "function",
      )
    ) {
      throw new ZangpuSigningError("invalid signer dependency");
    }
    this.#keyId = keyId;
    this.#secret = secret;
    this.#clock = clock;
    this.#nonceFactory = nonceFactory;
    this.#requestIdFactory = requestIdFactory;
  }

  sign({ method, path, query = "", body, requestId, nonce, timestamp }) {
    if (!(body instanceof Uint8Array)) {
      throw new ZangpuSigningError("body must be raw bytes");
    }
    const resolvedTimestamp =
      timestamp === undefined ? Math.trunc(this.#clock()) : timestamp;
    if (!Number.isSafeInteger(resolvedTimestamp) || resolvedTimestamp < 0) {
      throw new ZangpuSigningError("invalid timestamp");
    }
    const timestampText = String(resolvedTimestamp);
    const resolvedNonce = nonce ?? this.#nonceFactory();
    const resolvedRequestId = requestId ?? this.#requestIdFactory();
    const canonical = createCanonicalRequest({
      method,
      path,
      query,
      bodyHash: bodySha256Hex(body),
      keyId: this.#keyId,
      timestamp: timestampText,
      nonce: resolvedNonce,
      requestId: resolvedRequestId,
    });
    const signature = createHmac("sha256", Buffer.from(this.#secret, "utf8"))
      .update(canonical, "utf8")
      .digest("hex");
    return new SignedHeaders({
      keyId: this.#keyId,
      timestamp: timestampText,
      nonce: resolvedNonce,
      requestId: resolvedRequestId,
      signature,
    });
  }

  toJSON() {
    return { keyId: this.#keyId, secret: "<redacted>" };
  }
}
