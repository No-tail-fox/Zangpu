export type JsonPrimitive = string | number | boolean | null;
export type JsonValue =
  JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export interface RateLimit {
  readonly limit: number;
  readonly remaining: number;
  readonly resetAt: number;
}

export interface ZangpuResponse<T extends JsonObject = JsonObject> {
  readonly data: T;
  readonly requestId: string;
  readonly rateLimit: RateLimit;
}

export interface ZangpuStreamEvent<T extends JsonObject = JsonObject> {
  readonly data: T;
  readonly requestId: string;
}

export interface SignerOptions {
  keyId: string;
  secret: string;
  clock?: () => number;
  nonceFactory?: () => string;
  requestIdFactory?: () => string;
}

export interface SignOptions {
  method: string;
  path: string;
  query?: string;
  body: Uint8Array;
  requestId?: string;
  nonce?: string;
  timestamp?: number;
}

export class ZangpuSigningError extends TypeError {
  constructor(message: string);
}

export interface SignedHeadersOptions {
  keyId: string;
  timestamp: string;
  nonce: string;
  requestId: string;
  signature: string;
}

export class SignedHeaders {
  constructor(options: SignedHeadersOptions);
  readonly keyId: string;
  readonly timestamp: string;
  readonly nonce: string;
  readonly requestId: string;
  readonly signature: string;
  readonly headers: Readonly<Record<string, string>>;
  toJSON(): {
    keyId: string;
    requestId: string;
    nonce: "<redacted>";
    signature: "<redacted>";
  };
}

export class ZangpuSigner {
  constructor(options: SignerOptions);
  sign(options: SignOptions): SignedHeaders;
  toJSON(): { keyId: string; secret: "<redacted>" };
}

export class ZangpuError extends Error {
  constructor(message?: string);
}
export class ZangpuTransportError extends ZangpuError {
  constructor(code: string, options?: { retryable?: boolean });
  readonly code: string;
  readonly retryable: boolean;
}
export class ZangpuProtocolError extends ZangpuError {
  constructor();
  readonly code: "PROTOCOL_ERROR";
  readonly retryable: false;
}
export class ZangpuAPIError extends ZangpuError {
  constructor(options: {
    statusCode: number;
    code: string;
    message: string;
    requestId: string | null;
    retryable: boolean;
    operationId?: string | null;
  });
  readonly statusCode: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly retryable: boolean;
  readonly operationId: string | null;
}

export interface ChatMessage extends JsonObject {
  role: string;
  content: JsonValue;
}

export interface ChatOptions {
  model: string;
  messages: ChatMessage[];
  temperature?: number;
  topP?: number;
  maxTokens?: number;
  maxCompletionTokens?: number;
  stop?: string | string[];
  requestId?: string;
}

export interface ZangpuClientOptions extends SignerOptions {
  baseUrl: string;
  timeoutMs?: number;
  fetch?: typeof globalThis.fetch;
}

export class ZangpuClient {
  constructor(options: ZangpuClientOptions);
  listModels(options?: { requestId?: string }): Promise<ZangpuResponse>;
  getUsage(options?: { requestId?: string }): Promise<ZangpuResponse>;
  chatCompletions(options: ChatOptions): Promise<ZangpuResponse>;
  streamChatCompletions(
    options: ChatOptions,
  ): AsyncGenerator<ZangpuStreamEvent, void, undefined>;
  toJSON(): { baseUrl: string; signer: ZangpuSigner };
}

export const HMAC_ALGORITHM: "ZANGPU-HMAC-SHA256";
export const SIGNATURE_VERSION: "1";
export const SDK_VERSION: "0.1.0";
export const MAX_REQUEST_BYTES: number;
export const MAX_JSON_RESPONSE_BYTES: number;
export const MAX_SSE_EVENT_BYTES: number;
export function bodySha256Hex(body: Uint8Array): string;
export function canonicalizePath(rawPath: string): string;
export function canonicalizeQuery(rawQuery: string): string;
export function createCanonicalRequest(options: {
  method: string;
  path: string;
  query?: string;
  bodyHash: string;
  keyId: string;
  timestamp: string;
  nonce: string;
  requestId: string;
}): string;
