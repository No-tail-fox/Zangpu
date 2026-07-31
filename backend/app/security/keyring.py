import base64
import binascii
import json
import re
from dataclasses import dataclass
from secrets import token_bytes

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import SecretStr

KEY_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_KEY_VERSIONS = 16
AES_256_KEY_BYTES = 32
AES_GCM_NONCE_BYTES = 12


class KeyringConfigurationError(ValueError):
    pass


class KeyVersionUnavailable(RuntimeError):
    pass


class CredentialDecryptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True, repr=False)
class EncryptedSecret:
    ciphertext: str
    nonce: str
    key_id: str

    def __repr__(self) -> str:
        return f"EncryptedSecret(key_id={self.key_id!r}, ciphertext=<redacted>, nonce=<redacted>)"


def credential_aad(credential_id: str, api_client_id: str, key_id: str, master_key_id: str) -> bytes:
    values = (credential_id, api_client_id, key_id, master_key_id)
    if any(not value or "\n" in value or "\r" in value for value in values):
        raise ValueError("credential AAD identifiers must be non-empty single-line values")
    return "\n".join(values).encode("utf-8")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise KeyringConfigurationError("credential key ring contains duplicate versions")
        value[key] = item
    return value


def _decode_key(encoded_key: str) -> bytes:
    try:
        key = base64.b64decode(encoded_key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyringConfigurationError("credential key ring contains invalid base64") from exc
    if len(key) != AES_256_KEY_BYTES:
        raise KeyringConfigurationError("credential key ring values must decode to 32 bytes")
    return key


class CredentialKeyring:
    __slots__ = ("_active_key_id", "_keys")

    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None:
        self._keys = dict(keys)
        self._active_key_id = active_key_id

    def __repr__(self) -> str:
        versions = tuple(sorted(self._keys))
        return f"CredentialKeyring(active_key_id={self._active_key_id!r}, versions={versions!r}, keys=<redacted>)"

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    @classmethod
    def from_json(cls, serialized: SecretStr, *, active_key_id: str) -> "CredentialKeyring":
        if not KEY_VERSION_RE.fullmatch(active_key_id):
            raise KeyringConfigurationError("invalid active credential key version")
        try:
            parsed = json.loads(serialized.get_secret_value(), object_pairs_hook=_unique_json_object)
        except KeyringConfigurationError:
            raise
        except (json.JSONDecodeError, TypeError) as exc:
            raise KeyringConfigurationError("credential key ring must be a JSON object") from exc
        if not isinstance(parsed, dict) or not 1 <= len(parsed) <= MAX_KEY_VERSIONS:
            raise KeyringConfigurationError("credential key ring must contain 1 to 16 versions")

        keys: dict[str, bytes] = {}
        for key_id, encoded_key in parsed.items():
            if not isinstance(key_id, str) or not KEY_VERSION_RE.fullmatch(key_id):
                raise KeyringConfigurationError("invalid credential key version")
            if not isinstance(encoded_key, str):
                raise KeyringConfigurationError("credential key values must be base64 strings")
            keys[key_id] = _decode_key(encoded_key)
        if active_key_id not in keys:
            raise KeyringConfigurationError("active credential key version is unavailable")
        return cls(keys, active_key_id)

    def encrypt(self, plaintext: bytes, *, aad: bytes) -> EncryptedSecret:
        if not plaintext:
            raise ValueError("credential plaintext must not be empty")
        nonce = token_bytes(AES_GCM_NONCE_BYTES)
        ciphertext = AESGCM(self._keys[self._active_key_id]).encrypt(nonce, plaintext, aad)
        return EncryptedSecret(
            ciphertext=base64.b64encode(ciphertext).decode("ascii"),
            nonce=base64.b64encode(nonce).decode("ascii"),
            key_id=self._active_key_id,
        )

    def decrypt(self, encrypted: EncryptedSecret, *, aad: bytes) -> bytes:
        key = self._keys.get(encrypted.key_id)
        if key is None:
            raise KeyVersionUnavailable("credential key version is unavailable")
        try:
            nonce = base64.b64decode(encrypted.nonce, validate=True)
            ciphertext = base64.b64decode(encrypted.ciphertext, validate=True)
            if len(nonce) != AES_GCM_NONCE_BYTES:
                raise ValueError("invalid nonce length")
            return AESGCM(key).decrypt(nonce, ciphertext, aad)
        except (binascii.Error, InvalidTag, ValueError) as exc:
            raise CredentialDecryptionError("credential ciphertext could not be authenticated") from exc
