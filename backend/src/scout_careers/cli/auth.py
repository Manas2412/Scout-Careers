"""``scout-careers auth gmail`` — the one-time OAuth consent.

Run once per Google account, and again only after a revocation. It opens a
browser, waits for the operator to consent, and writes the resulting refresh
token to disk encrypted.

The command prints a fifteen-minute console walkthrough when there is no OAuth
client configured, because that is what is actually missing when this fails for
the first time, and "invalid client configuration" sends the operator to a
search engine rather than to the screen they need.

Nothing this command prints contains a token, an access token, an authorisation
code or the client secret. The success path prints the scopes granted, the file
the token went to, and its mode.
"""

from __future__ import annotations

from typing import Annotated

import typer

from scout_careers.cli.output import echo, error
from scout_careers.common.config import get_settings
from scout_careers.common.errors import (
    GmailClientSecretsMissing,
    MailTokenKeyMissing,
    MailTokenUnreadable,
)
from scout_careers.common.logging import configure_logging
from scout_careers.mail.oauth import authorise_gmail, client_secrets_guidance, store_token
from scout_careers.mail.scopes import PHASE_1_SCOPES
from scout_careers.mail.tokens import TOKEN_FILE_MODE, TokenStore

app = typer.Typer(no_args_is_help=True, help="Authorise Scout Careers against Google.")


@app.command("gmail")
def gmail(
    port: Annotated[
        int,
        typer.Option(
            "--port",
            help=(
                "Loopback port for the OAuth redirect. 0 picks an ephemeral one. "
                "On a headless host, pass the port forwarded over SSH."
            ),
        ),
    ] = 0,
    no_browser: Annotated[
        bool,
        typer.Option(
            "--no-browser",
            help="Print the consent URL instead of opening a browser. The headless case.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Re-authorise even though a token is already stored."),
    ] = False,
) -> None:
    """Run the desktop OAuth flow and store the refresh token.

    Phase 1 requests exactly one scope, ``gmail.readonly``. The digest's
    ``gmail.send`` is Phase 2's and is not requested now.

    On a headless VM, forward the loopback port from the laptop first:

        ssh -L 8765:127.0.0.1:8765 scout-prod
        scout-careers auth gmail --port 8765 --no-browser

    then open the printed URL in the laptop's browser.
    """
    settings = get_settings()
    configure_logging(settings)

    if not settings.mail_enabled:
        error(
            "MAIL_ENABLED is false, so the mail module is switched off and a stored "
            "grant would never be used. Set MAIL_ENABLED=true in .env first."
        )
        raise typer.Exit(code=1)

    store = TokenStore.from_settings(settings)
    if store.exists() and not force:
        error(
            f"A Gmail token is already stored at {store.path}. "
            "Re-authorise with --force, which overwrites it."
        )
        raise typer.Exit(code=1)

    echo(f"Requesting {len(PHASE_1_SCOPES)} scope(s):")
    for scope in PHASE_1_SCOPES:
        echo(f"  {scope}")
    echo()

    try:
        token = authorise_gmail(settings, port=port, open_browser=not no_browser)
        store_token(settings, token)
    except GmailClientSecretsMissing as exc:
        error(exc.message)
        raise typer.Exit(code=2) from exc
    except MailTokenKeyMissing as exc:
        # The grant succeeded and is being discarded on purpose: a token this
        # process cannot encrypt is a token it will not write.
        error(exc.message)
        error("The authorisation succeeded but nothing was stored. Set MAIL_TOKEN_KEY and re-run.")
        raise typer.Exit(code=2) from exc
    except MailTokenUnreadable as exc:
        error(exc.message)
        raise typer.Exit(code=1) from exc

    echo()
    echo("Refresh token obtained.")
    echo(f"Token written to {store.path} (mode {oct(TOKEN_FILE_MODE)[2:]}, Fernet-encrypted).")
    echo(f"Scopes granted: {', '.join(token.scopes)}")
    echo()
    echo("Discovery runs will now read the alert mailbox. Check with:")
    echo("  scout-careers run discovery --dry-run")


@app.command("status")
def status() -> None:
    """Report whether Gmail is configured and whether a token is stored.

    Says nothing about the token itself — not its value, not its age, not its
    length. Only whether the file exists and whether this configuration could
    decrypt one.
    """
    settings = get_settings()
    store = TokenStore.from_settings(settings)
    echo(f"MAIL_ENABLED             {str(settings.mail_enabled).lower()}")
    echo(f"OAuth client configured  {str(settings.oauth_client_configured).lower()}")
    echo(f"Encryption key present   {str(store.is_encryptable).lower()}")
    echo(f"Token path               {store.path}")
    echo(f"Token stored             {str(store.exists()).lower()}")
    echo(f"Scopes requested         {', '.join(PHASE_1_SCOPES)}")
    if not settings.gmail_configured:
        echo()
        echo("Gmail is not configured. mail_alert sources will be reported `disabled`.")
        if not settings.oauth_client_configured:
            echo()
            echo(client_secrets_guidance())


__all__ = ["app"]
