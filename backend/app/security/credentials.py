from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe

from backend.app.models.base import new_uuid, utc_epoch_seconds
from backend.app.models.credentials import ApiClientCredential
from backend.app.security.keyring import (
    CredentialDecryptionError,
    CredentialKeyring,
    EncryptedSecret,
    credential_aad,
)


class OneTimeSecretAlreadyRead(RuntimeError):
    pass


class CredentialCreationResult:
    __slots__ = ("_secret", "credential")

    def __init__(self, credential: ApiClientCredential, secret: str) -> None:
        self.credential = credential
        self._secret: str | None = secret

    def __repr__(self) -> str:
        return f"CredentialCreationResult(credential_id={self.credential.id!r}, secret=<redacted>)"

    def take_secret(self) -> str:
        if self._secret is None:
            raise OneTimeSecretAlreadyRead("credential Secret has already been read")
        secret = self._secret
        self._secret = None
        return secret


def _secret_fingerprint(secret: str) -> str:
    return sha256(secret.encode("utf-8")).hexdigest()


def create_protected_credential(
    keyring: CredentialKeyring,
    *,
    api_client_id: str,
    created_by: str,
    credential_id: str | None = None,
    key_id: str | None = None,
    now: int | None = None,
    expires_at: int | None = None,
) -> CredentialCreationResult:
    resolved_credential_id = credential_id or new_uuid()
    resolved_key_id = key_id or f"zpk_{token_urlsafe(24)}"
    created_at = utc_epoch_seconds() if now is None else now
    secret = f"zps_{token_urlsafe(32)}"
    master_key_id = keyring.active_key_id
    encrypted = keyring.encrypt(
        secret.encode("utf-8"),
        aad=credential_aad(resolved_credential_id, api_client_id, resolved_key_id, master_key_id),
    )
    credential = ApiClientCredential(
        id=resolved_credential_id,
        api_client_id=api_client_id,
        key_id=resolved_key_id,
        secret_ciphertext=encrypted.ciphertext,
        secret_nonce=encrypted.nonce,
        master_key_id=encrypted.key_id,
        secret_fingerprint=_secret_fingerprint(secret),
        status="active",
        expires_at=expires_at,
        created_by=created_by,
        created_at=created_at,
    )
    return CredentialCreationResult(credential, secret)


def decrypt_credential_secret(credential: ApiClientCredential, keyring: CredentialKeyring) -> str:
    encrypted = EncryptedSecret(
        ciphertext=credential.secret_ciphertext,
        nonce=credential.secret_nonce,
        key_id=credential.master_key_id,
    )
    plaintext = keyring.decrypt(
        encrypted,
        aad=credential_aad(credential.id, credential.api_client_id, credential.key_id, credential.master_key_id),
    )
    try:
        secret = plaintext.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CredentialDecryptionError("credential plaintext is invalid") from exc
    if not compare_digest(_secret_fingerprint(secret), credential.secret_fingerprint):
        raise CredentialDecryptionError("credential fingerprint does not match")
    return secret
