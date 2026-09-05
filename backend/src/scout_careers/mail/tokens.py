"""The encrypted-at-rest OAuth token store (EMAIL_INGESTION.md §2.5).

The refresh token is the highest-value secret in the system: it is read access
to the operator's entire mailbox, and — once Phase 2 adds ``gmail.send`` — the
ability to send as them (SECURITY_ARCHITECTURE.md §7.1, asset A3). Everything in
this module follows from that single fact.

- **No plaintext fallback.** If ``MAIL_TOKEN_KEY`` is absent, :meth:`TokenStore.save`
  raises :class:`~scout_careers.common.errors.MailTokenKeyMissing` and writes
  nothing. A fallback that stores the token in clear "just this once" is a
  fallback that runs on the one machine where it matters.
- **The write is atomic and the mode is set before the file is visible.** A
  temporary file in the same directory is created at ``0600``, written,
  ``fsync``-ed, and only then renamed over the target. A reader can never see a
  half-written token, and the token is never briefly world-readable between
  ``write`` and ``chmod``.
- **The secret cannot leak through a repr.** :class:`OAuthToken` holds every
  sensitive field as a pydantic ``SecretStr``, so ``repr``, ``str``,
  ``model_dump()`` and any structured log payload that swallows the object all
  render ``'**********'``. Serialisation for disk goes through
  :meth:`OAuthToken.to_payload`, which is called in exactly one place.
- **No exception message quotes the ciphertext or the key.** A missing file, a
  truncated file and a wrong key are one error with one remedy:
  ``scout-careers auth gmail``.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from scout_careers.common.config import Settings
from scout_careers.common.errors import MailTokenKeyMissing, MailTokenUnreadable
from scout_careers.common.logging import get_logger

log = get_logger(__name__)

#: The one mode the token file may ever have.
TOKEN_FILE_MODE: Final[int] = 0o600

#: Refuse a file larger than this before decrypting it. A token document is a
#: few hundred bytes; anything larger is a wrong path or a corrupted file, and
#: reading it into memory to find that out is unnecessary.
MAX_TOKEN_FILE_BYTES: Final[int] = 64 * 1024

#: Bumped only if the on-disk document's shape changes. Present so that a future
#: change is a migration rather than a silent misparse.
TOKEN_FORMAT_VERSION: Final[int] = 1


class OAuthToken(BaseModel):
    """The credential, as it is held in memory and written to disk.

    Deliberately not ``google.oauth2.credentials.Credentials``: that class is
    the transport's concern, its repr is not ours to control, and keeping it out
    of here means the store can be tested without a Google library in the room.
    Conversion lives in :mod:`scout_careers.mail.oauth`, at the boundary.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    refresh_token: SecretStr
    """The long-lived grant. The reason this whole module exists."""

    client_id: str
    client_secret: SecretStr
    token_uri: str = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
    scopes: tuple[str, ...] = ()
    access_token: SecretStr | None = None
    """The one-hour token. Cached so a restart does not force a refresh; still a
    secret, still never logged."""

    expiry: datetime | None = None
    """When ``access_token`` stops working. UTC, tz-aware, or ``None``."""

    version: int = Field(default=TOKEN_FORMAT_VERSION)

    def to_payload(self) -> dict[str, Any]:
        """Render the document that is encrypted and written to disk.

        The only method that unwraps the secrets. Called by
        :meth:`TokenStore.save` and by nothing else, so "where does the token
        become a string" has exactly one answer.

        Returns:
            A JSON-serialisable dict carrying the cleartext values.
        """
        return {
            "version": self.version,
            "refresh_token": self.refresh_token.get_secret_value(),
            "client_id": self.client_id,
            "client_secret": self.client_secret.get_secret_value(),
            "token_uri": self.token_uri,
            "scopes": list(self.scopes),
            "access_token": (
                self.access_token.get_secret_value() if self.access_token is not None else None
            ),
            "expiry": self.expiry.isoformat() if self.expiry is not None else None,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OAuthToken:
        """Rebuild a token from a decrypted document.

        Args:
            payload: The decoded JSON document.

        Returns:
            The token.

        Raises:
            MailTokenUnreadable: When the document does not have the expected
                shape. The offending document is not quoted in the message.
        """
        try:
            return cls(
                refresh_token=SecretStr(payload["refresh_token"]),
                client_id=str(payload["client_id"]),
                client_secret=SecretStr(payload["client_secret"]),
                token_uri=str(payload.get("token_uri") or "https://oauth2.googleapis.com/token"),
                scopes=tuple(payload.get("scopes") or ()),
                access_token=(
                    SecretStr(payload["access_token"]) if payload.get("access_token") else None
                ),
                expiry=(
                    datetime.fromisoformat(payload["expiry"]) if payload.get("expiry") else None
                ),
                version=int(payload.get("version", TOKEN_FORMAT_VERSION)),
            )
        except (KeyError, TypeError, ValueError):
            # `from exc` is deliberately NOT used: a KeyError's argument is a
            # field name, but a ValueError from fromisoformat quotes the value,
            # and a chained traceback would carry it into the log.
            raise MailTokenUnreadable(
                "the stored token does not have the expected shape; "
                "re-authorise with `scout-careers auth gmail`"
            ) from None

    def with_access_token(self, token: str | None, expiry: datetime | None) -> OAuthToken:
        """Return a copy carrying a freshly refreshed access token.

        Args:
            token: The new access token, or ``None``.
            expiry: Its expiry.

        Returns:
            A new frozen token; the refresh token is unchanged.
        """
        return self.model_copy(
            update={
                "access_token": SecretStr(token) if token else None,
                "expiry": expiry,
            }
        )


class TokenStore:
    """One encrypted file, one operator.

    Args:
        path: Where the ciphertext lives. Outside the repository and outside
            every Docker build context.
        key: The Fernet key. ``None`` is a supported *state*, not a supported
            *operation*: the store can be constructed without one so that a
            caller can report the situation cleanly, but every read and write
            then refuses.
    """

    def __init__(self, *, path: Path, key: str | None) -> None:
        self._path = path
        self._key = key

    # -- construction ------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenStore:
        """Build the store the running configuration describes.

        Args:
            settings: Supplies ``mail_token_path`` and ``mail_token_key``.

        Returns:
            A store. Whether it can actually read or write is
            :attr:`is_encryptable`, deliberately separate from construction.
        """
        secret = settings.mail_token_key
        raw = secret.get_secret_value().strip() if secret is not None else ""
        return cls(path=settings.mail_token_path, key=raw or None)

    # -- introspection -----------------------------------------------------

    @property
    def path(self) -> Path:
        """Where the ciphertext lives. Not a secret; the key is elsewhere."""
        return self._path

    @property
    def is_encryptable(self) -> bool:
        """True when a key is present, so a save would encrypt rather than refuse."""
        return self._key is not None

    def exists(self) -> bool:
        """True when a token file is present. Says nothing about its contents."""
        return self._path.is_file()

    def __repr__(self) -> str:
        """Render the store without saying anything about the key or the token."""
        return f"TokenStore(path={str(self._path)!r}, encrypted={self.is_encryptable})"

    __str__ = __repr__

    # -- the two operations -------------------------------------------------

    def save(self, token: OAuthToken) -> None:
        """Encrypt and write the token atomically at mode ``0600``.

        Args:
            token: The credential to store.

        Raises:
            MailTokenKeyMissing: When no encryption key is configured. Nothing
                is written — refusing is the whole point, and a plaintext
                fallback here would be the only fallback that ever matters.
            OSError: When the destination directory cannot be created or
                written. Propagated: a token that was not stored must not look
                like a token that was.
        """
        fernet = self._fernet()
        ciphertext = fernet.encrypt(json.dumps(token.to_payload(), sort_keys=True).encode("utf-8"))

        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Written into the destination directory so the rename is atomic: a
        # cross-filesystem rename is a copy, and a copy is not atomic.
        handle, temporary = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".gmail-token-", suffix=".tmp"
        )
        temporary_path = Path(temporary)
        try:
            # mkstemp already creates at 0600; set it explicitly so the mode is
            # a stated property of this code rather than of the standard library.
            os.fchmod(handle, TOKEN_FILE_MODE)
            with os.fdopen(handle, "wb") as stream:
                stream.write(ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(self._path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise

        # Identifiers only. Not the value, not a prefix, not a length, not a
        # hash (SECURITY_ARCHITECTURE.md §7.2 rule 7).
        log.info("gmail_token_stored", path=str(self._path), mode=oct(TOKEN_FILE_MODE))

    def load(self) -> OAuthToken:
        """Read and decrypt the token.

        Returns:
            The stored credential.

        Raises:
            MailTokenKeyMissing: When no encryption key is configured.
            MailTokenUnreadable: When the file is absent, implausibly large,
                not decryptable with this key, or not the expected document.
                One error for four causes, because the remedy is the same one.
        """
        fernet = self._fernet()
        if not self._path.is_file():
            raise MailTokenUnreadable(
                f"no Gmail token at {self._path}; run `scout-careers auth gmail`"
            )
        size = self._path.stat().st_size
        if size > MAX_TOKEN_FILE_BYTES:
            raise MailTokenUnreadable(
                f"the file at {self._path} is {size} bytes, which is not a Gmail token"
            )
        try:
            plaintext = fernet.decrypt(self._path.read_bytes())
            payload = json.loads(plaintext.decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError):
            # Not chained, and the message names no bytes: `from exc` would put
            # the underlying library's text — which may quote the payload — into
            # the traceback that ends up in a log.
            raise MailTokenUnreadable(
                f"the Gmail token at {self._path} could not be decrypted with the "
                "configured MAIL_TOKEN_KEY; re-authorise with `scout-careers auth gmail`"
            ) from None
        if not isinstance(payload, dict):
            raise MailTokenUnreadable(
                f"the Gmail token at {self._path} is not a token document; re-authorise"
            )
        return OAuthToken.from_payload(payload)

    def delete(self) -> bool:
        """Remove the token file, if there is one.

        Returns:
            True when a file was removed. Used by the CLI when a re-authorisation
            replaces a grant that no longer works.
        """
        if not self._path.is_file():
            return False
        self._path.unlink()
        log.info("gmail_token_removed", path=str(self._path))
        return True

    # -- internals ---------------------------------------------------------

    def _fernet(self) -> Fernet:
        """Build the cipher, refusing when there is no key.

        Raises:
            MailTokenKeyMissing: When ``MAIL_TOKEN_KEY`` is unset or blank, or
                is not a valid Fernet key. Both are the same operator action.
        """
        if self._key is None:
            raise MailTokenKeyMissing(
                "MAIL_TOKEN_KEY is not set, so the Gmail token cannot be encrypted "
                "at rest and will not be written in clear. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        try:
            return Fernet(self._key.encode("utf-8"))
        except (ValueError, TypeError):
            raise MailTokenKeyMissing(
                "MAIL_TOKEN_KEY is not a valid Fernet key (32 url-safe base64-encoded "
                'bytes). Generate one with: python -c "from cryptography.fernet import '
                'Fernet; print(Fernet.generate_key().decode())"'
            ) from None


__all__ = [
    "MAX_TOKEN_FILE_BYTES",
    "TOKEN_FILE_MODE",
    "TOKEN_FORMAT_VERSION",
    "OAuthToken",
    "TokenStore",
]
