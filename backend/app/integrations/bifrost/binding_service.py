import json
from hashlib import sha256

from pydantic import SecretStr
from sqlalchemy.orm import Session

from backend.app.integrations.bifrost.models import (
    BifrostOutboxPayload,
    VirtualKeyCreationResult,
    VirtualKeySpec,
)
from backend.app.models.base import utc_epoch_seconds
from backend.app.models.bindings import ApiClientBinding
from backend.app.models.clients import ApiClient
from backend.app.models.outbox import ControlOutbox, queue_binding_sync
from backend.app.security.keyring import CredentialKeyring, EncryptedSecret


def binding_remote_fields_complete(binding: ApiClientBinding) -> bool:
    return bool(
        binding.zangpu_service_user_id
        and binding.bifrost_virtual_key_id
        and binding.bifrost_value_ciphertext
        and binding.bifrost_value_key_version
    )


def binding_aad(binding_id: str, api_client_id: str, virtual_key_id: str, master_key_id: str) -> bytes:
    values = (binding_id, api_client_id, virtual_key_id, master_key_id)
    if any(not value or "\n" in value or "\r" in value for value in values):
        raise ValueError("binding AAD identifiers must be non-empty single-line values")
    return "\n".join(values).encode("utf-8")


def desired_config_hash(spec: VirtualKeySpec) -> str:
    canonical = json.dumps(spec.create_payload(), ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("ascii")).hexdigest()


def stage_bifrost_binding_sync(
    session: Session,
    *,
    binding: ApiClientBinding,
    spec: VirtualKeySpec,
    action: str,
    idempotency_key: str,
    now: int | None = None,
) -> ControlOutbox:
    if action not in {"create", "update"}:
        raise ValueError("unsupported Bifrost binding action")
    if action == "create" and binding.bifrost_virtual_key_id is not None:
        raise ValueError("Bifrost binding already exists")
    if action == "update" and binding.bifrost_virtual_key_id is None:
        raise ValueError("Bifrost binding does not exist")
    config_hash = desired_config_hash(spec)
    payload = BifrostOutboxPayload(
        config_hash=config_hash,
        binding_version=binding.version + 1,
        desired=spec,
    ).model_dump(mode="json", exclude_none=True)
    return queue_binding_sync(
        session,
        binding=binding,
        desired_config_hash=config_hash,
        target="bifrost",
        action=action,
        idempotency_key=idempotency_key,
        payload=payload,
        now=now,
    )


def persist_created_binding(
    binding: ApiClientBinding,
    result: VirtualKeyCreationResult,
    keyring: CredentialKeyring,
    *,
    now: int | None = None,
) -> None:
    value = result.take_value().get_secret_value()
    master_key_id = keyring.active_key_id
    encrypted = keyring.encrypt(
        value.encode("utf-8"),
        aad=binding_aad(binding.id, binding.api_client_id, result.state.id, master_key_id),
    )
    binding.bifrost_virtual_key_id = result.state.id
    binding.bifrost_value_ciphertext = json.dumps(
        {"ciphertext": encrypted.ciphertext, "nonce": encrypted.nonce}, separators=(",", ":"), sort_keys=True
    )
    binding.bifrost_value_key_version = encrypted.key_id
    if binding_remote_fields_complete(binding):
        binding.sync_status = "active" if result.state.is_active else "disabled"
    else:
        binding.sync_status = "pending"
    binding.last_sync_error_code = None
    binding.updated_at = utc_epoch_seconds() if now is None else now


def decrypt_binding_value(binding: ApiClientBinding, keyring: CredentialKeyring) -> SecretStr:
    if not (
        binding.bifrost_virtual_key_id
        and binding.bifrost_value_ciphertext
        and binding.bifrost_value_key_version
    ):
        raise ValueError("binding has no protected Bifrost value")
    try:
        envelope = json.loads(binding.bifrost_value_ciphertext)
        encrypted = EncryptedSecret(
            ciphertext=envelope["ciphertext"],
            nonce=envelope["nonce"],
            key_id=binding.bifrost_value_key_version,
        )
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("binding ciphertext envelope is invalid") from exc
    plaintext = keyring.decrypt(
        encrypted,
        aad=binding_aad(
            binding.id,
            binding.api_client_id,
            binding.bifrost_virtual_key_id,
            binding.bifrost_value_key_version,
        ),
    )
    try:
        return SecretStr(plaintext.decode("utf-8", errors="strict"))
    except UnicodeDecodeError as exc:
        raise ValueError("binding plaintext is invalid") from exc


def stage_local_client_disable(
    session: Session,
    *,
    client: ApiClient,
    binding: ApiClientBinding,
    actor_id: str,
    idempotency_key: str,
    now: int | None = None,
) -> ControlOutbox:
    changed_at = utc_epoch_seconds() if now is None else now
    client.status = "disabled"
    client.disabled_at = changed_at
    client.updated_at = changed_at
    client.updated_by = actor_id
    client.version += 1
    for credential in client.credentials:
        if credential.status == "active":
            credential.status = "revoked"
            credential.revoked_by = actor_id
            credential.revoked_at = changed_at

    next_binding_version = binding.version + 1
    payload = BifrostOutboxPayload(
        config_hash=binding.bifrost_config_hash,
        binding_version=next_binding_version,
    ).model_dump(mode="json", exclude_none=True)
    return queue_binding_sync(
        session,
        binding=binding,
        desired_config_hash=binding.bifrost_config_hash,
        target="bifrost",
        action="disable",
        idempotency_key=idempotency_key,
        payload=payload,
        now=changed_at,
    )
