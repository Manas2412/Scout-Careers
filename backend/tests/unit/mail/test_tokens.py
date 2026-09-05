"""The encrypted token store.

Four properties, and each of them is the difference between "we encrypt the
token" and "the token is encrypted":

1. It round-trips, and what lands on disk is ciphertext — the plaintext secret
   does not appear anywhere in the file's bytes.
2. The file is mode ``0600``, and it got there atomically.
3. With no key, ``save`` **refuses**. It does not fall back to plaintext, and it
   does not leave a file behind.
4. The secret never appears in a ``repr``, a ``str``, or the text of any
   exception this module raises — including the ones raised while handling a
   corrupt file, which is exactly where a careless implementation leaks it.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from scout_careers.common.errors import MailTokenKeyMissing, MailTokenUnreadable
from scout_careers.mail.tokens import TOKEN_FILE_MODE, OAuthToken, TokenStore
from tests.conftest import make_settings


def store_at(path: Path, key: str | None) -> TokenStore:
    return TokenStore(path=path, key=key)


# --------------------------------------------------------------------------
# 1. Round trip, and the bytes on disk are ciphertext
# --------------------------------------------------------------------------


def test_save_then_load_round_trips(tmp_path: Path, fernet_key: str, token: OAuthToken) -> None:
    store = store_at(tmp_path / "gmail.token", fernet_key)
    store.save(token)
    loaded = store.load()

    assert loaded.refresh_token.get_secret_value() == token.refresh_token.get_secret_value()
    assert loaded.client_id == token.client_id
    assert loaded.scopes == token.scopes


def test_what_lands_on_disk_is_not_the_token(
    tmp_path: Path, fernet_key: str, token: OAuthToken, secret_value: str
) -> None:
    path = tmp_path / "gmail.token"
    store_at(path, fernet_key).save(token)

    written = path.read_bytes()
    assert secret_value.encode() not in written
    assert b"refresh_token" not in written
    # It is Fernet, so it decrypts with the key and only with the key.
    payload = json.loads(Fernet(fernet_key.encode()).decrypt(written))
    assert payload["refresh_token"] == secret_value


def test_an_access_token_and_expiry_survive(
    tmp_path: Path, fernet_key: str, token: OAuthToken
) -> None:
    expiry = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    store = store_at(tmp_path / "gmail.token", fernet_key)
    store.save(token.with_access_token("ya29.a0-not-a-real-token", expiry))

    loaded = store.load()
    assert loaded.expiry == expiry
    assert loaded.access_token is not None


def test_a_second_save_replaces_the_first(
    tmp_path: Path, fernet_key: str, token: OAuthToken
) -> None:
    path = tmp_path / "gmail.token"
    store = store_at(path, fernet_key)
    store.save(token)
    store.save(token.model_copy(update={"client_id": "second-client"}))

    assert store.load().client_id == "second-client"
    # Nothing left behind: an atomic write renames its temporary file away.
    assert [entry.name for entry in path.parent.iterdir()] == ["gmail.token"]


# --------------------------------------------------------------------------
# 2. Mode 0600
# --------------------------------------------------------------------------


def test_the_token_file_is_mode_600(tmp_path: Path, fernet_key: str, token: OAuthToken) -> None:
    path = tmp_path / "nested" / "gmail.token"
    store_at(path, fernet_key).save(token)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == TOKEN_FILE_MODE
    assert oct(mode) == "0o600"
    # No group or other bits, stated separately from the equality so a future
    # change to the constant cannot quietly widen this.
    assert not mode & (stat.S_IRWXG | stat.S_IRWXO)


# --------------------------------------------------------------------------
# 3. No key means no write. There is no plaintext fallback.
# --------------------------------------------------------------------------


def test_a_missing_key_refuses_to_store_rather_than_storing_plaintext(
    tmp_path: Path, token: OAuthToken, secret_value: str
) -> None:
    path = tmp_path / "gmail.token"
    store = store_at(path, None)

    with pytest.raises(MailTokenKeyMissing) as excinfo:
        store.save(token)

    assert not path.exists(), "nothing may be written when the token cannot be encrypted"
    assert list(tmp_path.iterdir()) == [], "not even a temporary file"
    assert secret_value not in str(excinfo.value)
    assert store.is_encryptable is False


def test_a_missing_key_refuses_to_load_too(tmp_path: Path, fernet_key: str) -> None:
    path = tmp_path / "gmail.token"
    path.write_bytes(Fernet(fernet_key.encode()).encrypt(b"{}"))

    with pytest.raises(MailTokenKeyMissing):
        store_at(path, None).load()


def test_a_key_that_is_not_a_fernet_key_is_refused(tmp_path: Path, token: OAuthToken) -> None:
    store = store_at(tmp_path / "gmail.token", "not-a-fernet-key")
    with pytest.raises(MailTokenKeyMissing) as excinfo:
        store.save(token)
    assert "Fernet" in str(excinfo.value)
    assert not (tmp_path / "gmail.token").exists()


def test_blank_settings_key_is_treated_as_absent(tmp_path: Path) -> None:
    settings = make_settings(mail_token_path=tmp_path / "gmail.token", mail_token_key="   ")
    assert settings.mail_token_key is None
    assert TokenStore.from_settings(settings).is_encryptable is False


def test_from_settings_uses_the_configured_path_and_key(tmp_path: Path, fernet_key: str) -> None:
    settings = make_settings(mail_token_path=tmp_path / "gmail.token", mail_token_key=fernet_key)
    store = TokenStore.from_settings(settings)
    assert store.path == tmp_path / "gmail.token"
    assert store.is_encryptable is True


# --------------------------------------------------------------------------
# 4. The secret never leaves through a repr, a str, or an exception
# --------------------------------------------------------------------------


def test_the_token_is_not_in_its_own_repr_or_str(token: OAuthToken, secret_value: str) -> None:
    for rendering in (repr(token), str(token), f"{token}"):
        assert secret_value not in rendering
        assert "**********" in rendering


def test_the_token_is_not_in_a_model_dump(token: OAuthToken, secret_value: str) -> None:
    # A structured log line that swallowed the object whole must still be safe.
    assert secret_value not in str(token.model_dump())
    assert secret_value not in json.dumps(token.model_dump(mode="json"), default=str)


def test_the_store_repr_says_nothing_about_the_key(tmp_path: Path, fernet_key: str) -> None:
    store = store_at(tmp_path / "gmail.token", fernet_key)
    for rendering in (repr(store), str(store)):
        assert fernet_key not in rendering
        assert "encrypted=True" in rendering


def test_a_corrupt_file_raises_without_quoting_its_bytes(
    tmp_path: Path, fernet_key: str, token: OAuthToken, secret_value: str
) -> None:
    path = tmp_path / "gmail.token"
    # Encrypted with a different key: the ciphertext is real, and undecryptable.
    other = Fernet.generate_key().decode()
    store_at(path, other).save(token)

    with pytest.raises(MailTokenUnreadable) as excinfo:
        store_at(path, fernet_key).load()

    rendered = f"{excinfo.value}\n{excinfo.value!r}"
    assert secret_value not in rendered
    assert path.read_text(encoding="utf-8", errors="replace")[:40] not in rendered
    # And the underlying InvalidToken is not chained in, so a traceback carries
    # nothing either.
    assert excinfo.value.__cause__ is None


def test_a_missing_file_names_the_remedy(tmp_path: Path, fernet_key: str) -> None:
    with pytest.raises(MailTokenUnreadable) as excinfo:
        store_at(tmp_path / "absent.token", fernet_key).load()
    assert "scout-careers auth gmail" in str(excinfo.value)


def test_an_implausibly_large_file_is_refused_before_decryption(
    tmp_path: Path, fernet_key: str
) -> None:
    path = tmp_path / "gmail.token"
    path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(MailTokenUnreadable):
        store_at(path, fernet_key).load()


def test_a_decryptable_but_wrong_shaped_document_is_refused(
    tmp_path: Path, fernet_key: str
) -> None:
    path = tmp_path / "gmail.token"
    path.write_bytes(Fernet(fernet_key.encode()).encrypt(b'{"nothing": "useful"}'))
    with pytest.raises(MailTokenUnreadable):
        store_at(path, fernet_key).load()


def test_a_decryptable_non_object_is_refused(tmp_path: Path, fernet_key: str) -> None:
    path = tmp_path / "gmail.token"
    path.write_bytes(Fernet(fernet_key.encode()).encrypt(b'["a", "list"]'))
    with pytest.raises(MailTokenUnreadable):
        store_at(path, fernet_key).load()


# --------------------------------------------------------------------------
# Housekeeping
# --------------------------------------------------------------------------


def test_exists_and_delete(tmp_path: Path, fernet_key: str, token: OAuthToken) -> None:
    store = store_at(tmp_path / "gmail.token", fernet_key)
    assert store.exists() is False
    assert store.delete() is False

    store.save(token)
    assert store.exists() is True
    assert store.delete() is True
    assert store.exists() is False


def test_from_payload_rejects_a_missing_refresh_token(secret_value: str) -> None:
    with pytest.raises(MailTokenUnreadable) as excinfo:
        OAuthToken.from_payload({"client_id": "x", "client_secret": "y"})
    assert secret_value not in str(excinfo.value)


def test_with_access_token_does_not_disturb_the_refresh_token(
    token: OAuthToken, secret_value: str
) -> None:
    rotated = token.with_access_token("new-access", datetime(2026, 9, 5, tzinfo=UTC))
    assert rotated.refresh_token.get_secret_value() == secret_value
    assert token.access_token is None, "the original is frozen"


def test_settings_treat_a_blank_client_secrets_path_as_unset() -> None:
    # `.env.example` declares every key, so an unset optional one is an empty
    # value — and Path("") is not "no file configured".
    settings = make_settings(gmail_client_secrets_path="", gmail_client_id="")
    assert settings.gmail_client_secrets_path is None
    assert settings.gmail_client_id is None
    assert settings.oauth_client_configured is False
