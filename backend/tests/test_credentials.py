import base64
import json

import pytest
from pydantic import SecretStr

from backend.app.models.credentials import CredentialSummary
from backend.app.security.credentials import (
    OneTimeSecretAlreadyRead,
    create_protected_credential,
    decrypt_credential_secret,
)
from backend.app.security.keyring import (
    CredentialDecryptionError,
    CredentialKeyring,
    EncryptedSecret,
    KeyringConfigurationError,
    KeyVersionUnavailable,
    credential_aad,
)


def encoded_key(value: int) -> str:
    return base64.b64encode(bytes([value]) * 32).decode("ascii")


def keyring_json(**versions: str) -> SecretStr:
    return SecretStr(json.dumps(versions, separators=(",", ":")))


def test_aes_gcm_rotation_aad_binding_and_tamper_rejection() -> None:
    old_ring = CredentialKeyring.from_json(keyring_json(v1=encoded_key(1)), active_key_id="v1")
    rotating_ring = CredentialKeyring.from_json(
        keyring_json(v1=encoded_key(1), v2=encoded_key(2)), active_key_id="v2"
    )
    aad_v1 = credential_aad("credential-1", "client-1", "zpk_public_1", "v1")
    encrypted_v1 = old_ring.encrypt(b"zps_rotation_test", aad=aad_v1)

    assert encrypted_v1.key_id == "v1"
    assert rotating_ring.decrypt(encrypted_v1, aad=aad_v1) == b"zps_rotation_test"

    encrypted_v2 = rotating_ring.encrypt(
        b"zps_active_test", aad=credential_aad("credential-2", "client-1", "zpk_public_2", "v2")
    )
    assert encrypted_v2.key_id == "v2"

    with pytest.raises(CredentialDecryptionError):
        rotating_ring.decrypt(
            encrypted_v1,
            aad=credential_aad("credential-other", "client-1", "zpk_public_1", "v1"),
        )

    ciphertext = bytearray(base64.b64decode(encrypted_v1.ciphertext))
    ciphertext[-1] ^= 1
    tampered = EncryptedSecret(
        ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        nonce=encrypted_v1.nonce,
        key_id=encrypted_v1.key_id,
    )
    with pytest.raises(CredentialDecryptionError):
        rotating_ring.decrypt(tampered, aad=aad_v1)

    new_only_ring = CredentialKeyring.from_json(keyring_json(v2=encoded_key(2)), active_key_id="v2")
    with pytest.raises(KeyVersionUnavailable):
        new_only_ring.decrypt(encrypted_v1, aad=aad_v1)


def test_keyring_rejects_invalid_keys_or_unknown_active_version() -> None:
    with pytest.raises(KeyringConfigurationError):
        CredentialKeyring.from_json(SecretStr("not-json"), active_key_id="v1")
    with pytest.raises(KeyringConfigurationError):
        CredentialKeyring.from_json(keyring_json(v1=base64.b64encode(b"short").decode()), active_key_id="v1")
    with pytest.raises(KeyringConfigurationError):
        CredentialKeyring.from_json(keyring_json(v1=encoded_key(1)), active_key_id="v2")


def test_created_secret_is_returned_once_and_never_enters_default_projection() -> None:
    keyring = CredentialKeyring.from_json(keyring_json(v1=encoded_key(1)), active_key_id="v1")
    result = create_protected_credential(
        keyring,
        api_client_id="client-1",
        created_by="admin-1",
        credential_id="credential-1",
        key_id="zpk_public_1",
        now=1_700_000_000,
    )
    secret = result.take_secret()
    credential = result.credential

    assert secret.startswith("zps_")
    assert secret not in repr(result)
    assert credential.secret_ciphertext not in repr(result)
    assert decrypt_credential_secret(credential, keyring) == secret
    with pytest.raises(OneTimeSecretAlreadyRead):
        result.take_secret()

    payload = CredentialSummary.model_validate(credential).model_dump()
    assert secret not in repr(payload)
    assert {
        "secret_ciphertext",
        "secret_nonce",
        "secret_fingerprint",
        "master_key_id",
    }.isdisjoint(payload)


def test_credential_factory_generates_public_and_internal_identifiers() -> None:
    keyring = CredentialKeyring.from_json(keyring_json(v1=encoded_key(1)), active_key_id="v1")
    result = create_protected_credential(
        keyring,
        api_client_id="client-1",
        created_by="admin-1",
        now=1_700_000_000,
    )

    assert len(result.credential.id) == 36
    assert result.credential.key_id.startswith("zpk_")
    assert 8 <= len(result.credential.key_id) <= 80
