export class ZangpuError extends Error {
  constructor(message) {
    super(message);
    this.name = new.target.name;
  }
}

export class ZangpuTransportError extends ZangpuError {
  constructor(code, { retryable = true } = {}) {
    super(`${code}: request transport failed`);
    this.code = code;
    this.retryable = retryable;
  }
}

export class ZangpuProtocolError extends ZangpuError {
  constructor() {
    super("PROTOCOL_ERROR: response contract validation failed");
    this.code = "PROTOCOL_ERROR";
    this.retryable = false;
  }
}

export class ZangpuAPIError extends ZangpuError {
  constructor({
    statusCode,
    code,
    message,
    requestId,
    retryable,
    operationId = null,
  }) {
    super(`${code}: ${message}`);
    this.statusCode = statusCode;
    this.code = code;
    this.requestId = requestId;
    this.retryable = retryable;
    this.operationId = operationId;
  }
}
