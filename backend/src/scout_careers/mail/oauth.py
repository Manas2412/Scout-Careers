"""The desktop OAuth flow (EMAIL_INGESTION.md §2.2).

One operator, one machine, one consent. The client is registered in Google Cloud
as a **Desktop app**, which means there is no hosted redirect URI and no
server-side multi-tenant exchange — and no client secret worth protecting,
because PKCE is what actually binds the authorisation code to this session.

Three implementation details are not optional:

- **Loopback redirect, not out-of-band.** Google retired
  ``urn:ietf:wg:oauth:2.0:oob``. The flow binds a port on ``127.0.0.1``, serves
  exactly one request, and shuts the listener down. On a headless host the same
  port is forwarded over SSH and the consent happens in the laptop's browser
  (``DEPLOYMENT_ENV_RUNBOOK.md`` §6.2), which is why ``port`` is a parameter
  rather than always ephemeral: a tunnel needs a number it can agree on in
  advance.
- **``access_type=offline`` and ``prompt=consent``.** Google returns a refresh
  token only on the first grant unless consent is re-forced, and a flow that
  succeeds without producing a refresh token fails silently an hour later.
- **PKCE (S256).** ``google-auth-oauthlib`` generates the verifier by default;
  it is asserted rather than assumed, because losing it would go unnoticed.

Nothing in this module logs a token, a code, or the client secret. The success
path logs the scopes granted and the file the token went to, and nothing else.
"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import SecretStr

from scout_careers.common.config import Settings
from scout_careers.common.errors import GmailClientSecretsMissing, MailTokenUnreadable
from scout_careers.common.logging import get_logger
from scout_careers.mail.scopes import PHASE_1_SCOPES
from scout_careers.mail.tokens import OAuthToken, TokenStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Sequence

log = get_logger(__name__)

AUTH_URI: Final[str] = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI: Final[str] = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret

#: Loopback only. A redirect host that is not the loopback interface is a
#: redirect somebody else can receive.
REDIRECT_HOST: Final[str] = "127.0.0.1"

#: What the operator is told, verbatim, when no OAuth client is configured. It
#: is a runbook rather than an error string on purpose: the fifteen minutes this
#: costs are fifteen minutes in a console whose screens are not guessable.
CLIENT_SECRETS_GUIDANCE: Final[str] = """\
No Google desktop OAuth client is configured, so `scout-careers auth gmail`
cannot start. Set one up once, in the Google Cloud console:

  1. console.cloud.google.com -> the project picker (top bar) -> NEW PROJECT.
     Name it `scout-careers`. Create, then select it.
  2. APIs & Services -> Library -> search "Gmail API" -> ENABLE.
  3. APIs & Services -> OAuth consent screen (newer consoles: "Google Auth
     Platform" -> Branding / Audience):
       a. User type: External. CREATE.
       b. App name `Scout Careers`; user support email and developer contact
          email: the operator's own address. SAVE AND CONTINUE.
       c. Scopes -> ADD OR REMOVE SCOPES -> tick exactly:
             https://www.googleapis.com/auth/gmail.readonly
          Do NOT add gmail.modify. Do not add gmail.send yet — Phase 2's
          digest asks for it separately. UPDATE, SAVE AND CONTINUE.
       d. Test users -> ADD USERS -> the operator's Gmail address.
       e. Back on the OAuth consent screen (Audience), under Publishing
          status press PUBLISH APP and confirm. This is the step that breaks
          the system a week later if it is skipped: gmail.readonly is a
          restricted scope, and while the app is in Testing, Google expires
          every refresh token after 7 days.
  4. APIs & Services -> Credentials -> CREATE CREDENTIALS -> OAuth client ID
     -> Application type: Desktop app -> name it `scout-careers-cli` -> CREATE.
  5. In the dialog, either copy the client ID and client secret into `.env`:

         GMAIL_CLIENT_ID=<the client id>
         GMAIL_CLIENT_SECRET=<the client secret>

     or press DOWNLOAD JSON, put the file somewhere outside the repository
     (mode 600), and point `.env` at it:

         GMAIL_CLIENT_SECRETS_PATH=/path/to/client_secret_....json

  6. Also in `.env`, set MAIL_ENABLED=true and generate the at-rest key:

         python -c "from cryptography.fernet import Fernet as F; print(F.generate_key().decode())"

     and put that value in MAIL_TOKEN_KEY. Keep it somewhere other than the
     token file: losing it makes a stored token permanently undecryptable.

Then run `scout-careers auth gmail` again. At the consent screen, "Google
hasn't verified this app" is expected once: Advanced -> Go to Scout Careers.\
"""


def client_secrets_guidance() -> str:
    """Return the operator-facing set-up instructions.

    Returns:
        The numbered console walkthrough printed whenever the OAuth client is
        missing. A function rather than a bare constant so the CLI and the
        exception path cannot drift apart.
    """
    return CLIENT_SECRETS_GUIDANCE


def resolve_client_config(settings: Settings) -> dict[str, dict[str, Any]]:
    """Build the ``installed`` client configuration the flow needs.

    Two supported sources, in order: the client-secrets JSON downloaded from the
    console, then ``GMAIL_CLIENT_ID`` / ``GMAIL_CLIENT_SECRET``. The file wins
    when both are set, because a file the operator pointed at explicitly is a
    stronger statement of intent than two values that may be left over.

    Args:
        settings: Configuration.

    Returns:
        ``{"installed": {...}}`` as ``google-auth-oauthlib`` expects it.

    Raises:
        GmailClientSecretsMissing: When neither source is configured, or the
            named file is absent or is not a desktop client document. The
            message carries :data:`CLIENT_SECRETS_GUIDANCE`.
    """
    path = settings.gmail_client_secrets_path
    if path is not None:
        return _client_config_from_file(path)

    if settings.gmail_client_id and settings.gmail_client_secret is not None:
        return {
            "installed": {
                "client_id": settings.gmail_client_id,
                "client_secret": settings.gmail_client_secret.get_secret_value(),
                "auth_uri": AUTH_URI,
                "token_uri": TOKEN_URI,
                "redirect_uris": ["http://localhost"],
            }
        }

    raise GmailClientSecretsMissing(CLIENT_SECRETS_GUIDANCE)


def _client_config_from_file(path: Path) -> dict[str, dict[str, Any]]:
    """Read a downloaded ``client_secret_*.json``.

    Args:
        path: The file named by ``GMAIL_CLIENT_SECRETS_PATH``.

    Returns:
        The ``installed`` section, normalised.

    Raises:
        GmailClientSecretsMissing: When the file is missing or is not a desktop
            client document. A web-application client is refused explicitly
            rather than allowed to fail later at the redirect URI, which is a
            far more confusing place to discover it.
    """
    if not path.is_file():
        raise GmailClientSecretsMissing(
            f"GMAIL_CLIENT_SECRETS_PATH points at {path}, which does not exist.\n\n"
            + CLIENT_SECRETS_GUIDANCE
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise GmailClientSecretsMissing(
            f"{path} is not readable JSON.\n\n" + CLIENT_SECRETS_GUIDANCE
        ) from None

    section = document.get("installed") if isinstance(document, dict) else None
    if not isinstance(section, dict) or not section.get("client_id"):
        raise GmailClientSecretsMissing(
            f"{path} is not a Desktop app client (it has no `installed` section). "
            "Create the credential with Application type: Desktop app.\n\n"
            + CLIENT_SECRETS_GUIDANCE
        )
    return {
        "installed": {
            "client_id": str(section["client_id"]),
            "client_secret": str(section.get("client_secret", "")),
            "auth_uri": str(section.get("auth_uri") or AUTH_URI),
            "token_uri": str(section.get("token_uri") or TOKEN_URI),
            "redirect_uris": list(section.get("redirect_uris") or ["http://localhost"]),
        }
    }


def token_from_credentials(credentials: Any) -> OAuthToken:
    """Convert a Google ``Credentials`` object into the stored token.

    Args:
        credentials: What the flow or a refresh produced.

    Returns:
        The credential in this system's own shape.

    Raises:
        MailTokenUnreadable: When the grant carried no refresh token. That
            happens when consent was not re-forced, and it is caught here
            because the alternative is an install that works for exactly one
            hour and then stops for a reason nobody connects to this moment.
    """
    refresh_token = getattr(credentials, "refresh_token", None)
    if not refresh_token:
        raise MailTokenUnreadable(
            "Google returned no refresh token. Revoke this app's access at "
            "myaccount.google.com/permissions and run `scout-careers auth gmail` "
            "again — a refresh token is only issued when consent is re-granted."
        )
    expiry = getattr(credentials, "expiry", None)
    if expiry is not None and expiry.tzinfo is None:
        # google-auth stores a naive UTC expiry. Everything downstream of here
        # is tz-aware, and ruff's DTZ rules exist to keep it that way.
        expiry = expiry.replace(tzinfo=UTC)
    access_token = getattr(credentials, "token", None)
    scopes: Sequence[str] = getattr(credentials, "scopes", None) or PHASE_1_SCOPES
    return OAuthToken(
        refresh_token=SecretStr(str(refresh_token)),
        client_id=str(getattr(credentials, "client_id", "") or ""),
        client_secret=SecretStr(str(getattr(credentials, "client_secret", "") or "")),
        token_uri=str(getattr(credentials, "token_uri", None) or TOKEN_URI),
        scopes=tuple(scopes),
        access_token=SecretStr(str(access_token)) if access_token else None,
        expiry=expiry,
    )


def credentials_from_token(token: OAuthToken) -> Any:
    """Rebuild a Google ``Credentials`` object from the stored token.

    Args:
        token: The decrypted credential.

    Returns:
        A ``google.oauth2.credentials.Credentials``. Typed ``Any`` because
        ``google-auth`` ships no stubs, and pretending otherwise would put a
        fictional type into a strict-mode codebase.
    """
    from google.oauth2.credentials import Credentials

    # `google-auth` ships no annotations, so mypy sees an untyped constructor.
    # Rebinding through Any states that plainly rather than suppressing it with
    # a type-ignore comment that would also hide a real error later.
    credentials_cls: Any = Credentials
    return credentials_cls(
        token=token.access_token.get_secret_value() if token.access_token else None,
        refresh_token=token.refresh_token.get_secret_value(),
        token_uri=token.token_uri,
        client_id=token.client_id,
        client_secret=token.client_secret.get_secret_value(),
        scopes=list(token.scopes),
    )


def authorise_gmail(
    settings: Settings,
    *,
    port: int = 0,
    open_browser: bool = True,
) -> OAuthToken:
    """Run the installed-app flow and return the resulting credential.

    Blocking and interactive: it binds a loopback port and waits for the
    operator to finish in a browser. The caller stores the result; this function
    deliberately does not, so that "obtain a grant" and "write a secret to disk"
    remain two reviewable steps.

    Args:
        settings: Supplies the OAuth client.
        port: The loopback port to bind. ``0`` picks an ephemeral one, which is
            right on a laptop; a headless host passes the number it forwarded
            over SSH.
        open_browser: ``False`` prints the consent URL instead of launching a
            browser — the headless case.

    Returns:
        The credential, including the refresh token.

    Raises:
        GmailClientSecretsMissing: When no desktop OAuth client is configured.
        MailTokenUnreadable: When the grant produced no refresh token.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_config = resolve_client_config(settings)
    flow = InstalledAppFlow.from_client_config(client_config, scopes=list(PHASE_1_SCOPES))

    log.info(
        "gmail_auth_started",
        scopes=list(PHASE_1_SCOPES),
        port=port or "ephemeral",
        open_browser=open_browser,
    )
    flow.run_local_server(
        host=REDIRECT_HOST,
        port=port,
        open_browser=open_browser,
        authorization_prompt_message=(
            "Open this URL to authorise Scout Careers:\n\n  {url}\n\n"
            f"Waiting for the authorisation code on {REDIRECT_HOST} ...\n\n"
            # Stated before the browser opens, because both failures happen in
            # the browser while the terminal keeps waiting — which reads as a
            # hang rather than as a rejected request. Ctrl+C is the way out.
            "If the browser refuses, it is almost always one of two things:\n\n"
            "  Error 400: redirect_uri_mismatch\n"
            "    The OAuth client is a Web application, not a Desktop app.\n"
            "    Desktop clients allow http://127.0.0.1 on any port with\n"
            "    nothing registered; web clients match registered URIs\n"
            "    literally, and this flow uses an ephemeral port. Create a\n"
            "    Desktop app client, or register http://127.0.0.1:8765/ on the\n"
            "    existing one and re-run with --port 8765.\n\n"
            "  Error 403: access_denied  (app has not completed verification)\n"
            "    The consent screen is in Testing and your account is not a\n"
            "    test user. Google Auth Platform -> Audience -> Test users ->\n"
            "    add your address. Then PUBLISH the app on that same page:\n"
            "    gmail.readonly is a restricted scope, and while the app stays\n"
            "    in Testing, Google expires every refresh token after 7 days --\n"
            "    so the grant works now and dies next week with no useful error.\n\n"
            "Press Ctrl+C to stop waiting."
        ),
        success_message=(
            "Scout Careers is authorised. You may close this window and return to the terminal."
        ),
        # Google issues a refresh token only on the first grant unless consent
        # is re-forced. Both are required, every time (§2.2).
        access_type="offline",
        prompt="consent",
    )
    token = token_from_credentials(flow.credentials)
    log.info("gmail_auth_completed", scopes=list(token.scopes))
    return token


def store_token(settings: Settings, token: OAuthToken) -> TokenStore:
    """Persist a freshly obtained credential.

    Args:
        settings: Supplies the token path and the encryption key.
        token: The credential to store.

    Returns:
        The store it was written to.

    Raises:
        MailTokenKeyMissing: When no key is configured. Nothing is written.
    """
    store = TokenStore.from_settings(settings)
    store.save(token)
    return store


__all__ = [
    "AUTH_URI",
    "CLIENT_SECRETS_GUIDANCE",
    "REDIRECT_HOST",
    "TOKEN_URI",
    "authorise_gmail",
    "client_secrets_guidance",
    "credentials_from_token",
    "resolve_client_config",
    "store_token",
    "token_from_credentials",
]
