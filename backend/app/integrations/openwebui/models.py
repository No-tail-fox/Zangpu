from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

SIGNED_BIGINT_MIN = -(2**63)
SIGNED_BIGINT_MAX = 2**63 - 1

BoundedText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=256,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]


class OpenWebUIUpstreamError(RuntimeError):
    def __init__(self, *, code: str, status_code: int, retryable: bool) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(f"{code}: Open WebUI internal request failed")


class OpenWebUIProtocolError(OpenWebUIUpstreamError):
    def __init__(self) -> None:
        super().__init__(
            code="OPENWEBUI_PROTOCOL_ERROR",
            status_code=502,
            retryable=True,
        )
        self.args = ("Open WebUI returned an invalid response contract",)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ServiceUserResolveRequest(_FrozenModel):
    external_client_id: UUID


class OperationReserveRequest(_FrozenModel):
    operation_id: UUID
    service_user_id: UUID
    model_id: BoundedText
    provider: BoundedText | None = None


class OperationSettleRequest(_FrozenModel):
    operation_id: UUID
    service_user_id: UUID
    prompt_tokens: StrictInt = Field(ge=0, le=SIGNED_BIGINT_MAX)
    completion_tokens: StrictInt = Field(ge=0, le=SIGNED_BIGINT_MAX)


class OperationActionRequest(_FrozenModel):
    operation_id: UUID
    service_user_id: UUID


class CreditAccountSnapshot(_FrozenModel):
    account_id: UUID
    service_user_id: UUID
    balance_micro: int = Field(ge=SIGNED_BIGINT_MIN, le=SIGNED_BIGINT_MAX)
    status: Literal["active", "frozen", "closed"]
    version: int = Field(ge=0, le=SIGNED_BIGINT_MAX)
    updated_at: int = Field(ge=0, le=SIGNED_BIGINT_MAX)


class ServiceUserResolution(_FrozenModel):
    external_client_id: UUID
    service_user_id: UUID
    created: bool
    account: CreditAccountSnapshot


class CreditOperationSnapshot(_FrozenModel):
    operation_id: UUID
    settlement_id: UUID
    service_user_id: UUID
    model_id: BoundedText
    provider: BoundedText | None = None
    status: Literal[
        "pending",
        "succeeded_charged",
        "cancelled_charged",
        "failed_no_charge",
        "settlement_error",
    ]
    prompt_tokens: int = Field(ge=0, le=SIGNED_BIGINT_MAX)
    completion_tokens: int = Field(ge=0, le=SIGNED_BIGINT_MAX)
    total_tokens: int = Field(ge=0, le=SIGNED_BIGINT_MAX)
    charged_micro: int = Field(ge=0, le=SIGNED_BIGINT_MAX)
    balance_after_micro: int | None = Field(
        default=None,
        ge=SIGNED_BIGINT_MIN,
        le=SIGNED_BIGINT_MAX,
    )
    account_version_after: int | None = Field(
        default=None,
        ge=0,
        le=SIGNED_BIGINT_MAX,
    )
    started_at: int = Field(ge=0, le=SIGNED_BIGINT_MAX)
    completed_at: int | None = Field(default=None, ge=0, le=SIGNED_BIGINT_MAX)
    updated_at: int = Field(ge=0, le=SIGNED_BIGINT_MAX)
    usage_operation_id: BoundedText | None = None
    account: CreditAccountSnapshot


class CreditOperationStatus(CreditOperationSnapshot):
    refunded: bool
    refund_ledger_id: UUID | None = None
    refunded_micro: int = Field(ge=0, le=SIGNED_BIGINT_MAX)


class CreditRefundSnapshot(_FrozenModel):
    operation_id: UUID
    settlement_id: UUID
    service_user_id: UUID
    refund_ledger_id: UUID
    refunded_micro: int = Field(gt=0, le=SIGNED_BIGINT_MAX)
    refunded_at: int = Field(ge=0, le=SIGNED_BIGINT_MAX)
    account: CreditAccountSnapshot


class OpenWebUIErrorDetail(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")


class OpenWebUIErrorEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    detail: OpenWebUIErrorDetail
