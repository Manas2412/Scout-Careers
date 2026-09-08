#!/usr/bin/env bash
#
# Why the mail_alert source found nothing. Read-only: it lists and reads, and
# sends nothing.
#
# The adapter asks Gmail for three things ANDed together — a label, an envelope
# sender from the allow-list, and a receipt window. When that returns zero, the
# run output says `empty` and cannot tell you which of the three was
# responsible. This runs them in isolation so the failing term names itself.
#
# The usual answer is the label. A Gmail filter applies only to mail arriving
# *after* it is created, unless "Also apply filter to N matching conversations"
# was ticked when saving — so alerts already in the mailbox carry no label, and
# a query that requires one matches none of them.
#
# Usage:  bash scripts/mail-probe.sh [lookback_hours]

set -euo pipefail
cd "$(dirname "$0")/.."

HOURS="${1:-72}" exec python - <<'PY'
import os
from datetime import timedelta

from scout_careers.cli._async import run as run_async
from scout_careers.common.clock import utcnow
from scout_careers.common.config import get_settings
from scout_careers.mail.gmail import build_mail_reader, close_mail_reader
from scout_careers.sources.base import MailAlertConfig


async def main() -> None:
    settings = get_settings()
    hours = int(os.environ.get("HOURS", "72"))
    since = utcnow() - timedelta(hours=hours)
    config = MailAlertConfig()
    senders = list(config.senders)

    reader = build_mail_reader(settings)
    if reader is None:
        print()
        print("  Mail is not configured: MAIL_ENABLED, the OAuth client and the")
        print("  stored token all have to be present. Try `scout-careers auth status`.")
        print()
        return

    print()
    print(f"  Window   last {hours}h (since {since:%Y-%m-%d %H:%M} UTC)")
    print(f"  Label    {config.label!r}")
    print(f"  Senders  {', '.join(senders)}")
    print()

    async def probe(caption: str, *, label: str, allow: list[str]) -> int:
        # An empty sender list means "read nothing" by design, so the
        # label-only probe passes a wildcard-ish list of the same senders and
        # relies on the label alone to differentiate. Both probes therefore
        # keep the client-side sender check honest.
        found = await reader.list_recent_messages(label=label, senders=allow, since=since)
        print(f"  {caption:<46} {len(found):>4}")
        return len(found)

    try:
        both = await probe("label AND senders  (what the adapter runs)", label=config.label, allow=senders)
        no_label = await probe("senders only, no label", label="", allow=senders)
    finally:
        await close_mail_reader(reader)

    print()
    if both:
        print("  The adapter's query matches mail. If a run still reports `empty`,")
        print("  the window is the difference: the source uses")
        print(f"  lookback_hours={config.lookback_hours}; this probe used {hours}.")
        print("  Re-run discovery with --force, or widen lookback_hours.")
    elif no_label:
        print(f"  {no_label} alert(s) are arriving. The LABEL is what is missing.")
        print()
        print("  Gmail filters do not apply retroactively. In Gmail:")
        print("    Settings > Filters and Blocked Addresses > edit the filter")
        print("    > Continue > tick 'Also apply filter to N matching")
        print("      conversations' > Update filter")
        print()
        print(f"  The label must be exactly {config.label!r}. A nested label such as")
        print(f"  'Jobs/{config.label}' is a different label to Gmail's `label:` operator.")
        print()
        print("  Note the Updates tab is irrelevant here — categories and labels are")
        print("  independent, and a filter labels mail whichever tab it lands in.")
    else:
        print("  Nothing matched, with or without the label. Either no alert has")
        print("  arrived in this window, or the mailbox this token is bound to is")
        print("  not the one receiving them. `scout-careers auth status` shows which.")
    print()


run_async(main())
PY
