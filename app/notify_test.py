"""Send a one-shot ntfy/Pushover probe without running the poller or Redis."""

from __future__ import annotations

import sys

from app.config import Settings
from app.escalation import NotifyError, build_notifier
from app.http import build_http_client
from app.logging_setup import setup_logging


def main() -> int:
    setup_logging("INFO")
    try:
        settings = Settings()
    except Exception as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2

    http = build_http_client()
    notifier = build_notifier(settings, http)
    try:
        print(f"Sending test alerts via {settings.notify_provider}...")
        notifier.send_emergency(
            title="TEST — New Grad SWE siren",
            message="If you see this, emergency/max-priority notify is working. You can ignore it.",
            url="https://www.instagram.com/stories/zero2sudo/",
        )
        print("  1/2 emergency (ntfy priority 5 / Pushover P2) sent")
        notifier.send_priority_low(
            title="[Queued] TEST — silent ping",
            message="Low-priority / quiet-hours style message. Should be much quieter than the first.",
        )
        print("  2/2 low-priority ping sent")
        if settings.notify_provider == "ntfy":
            print(
                f"Subscribe the iOS ntfy app to topic: {settings.ntfy_topic!r} "
                f"on {settings.ntfy_base_url.rstrip('/')}"
            )
        print("Check your phone.")
        return 0
    except NotifyError as exc:
        print(f"notify failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"notify failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        http.close()


if __name__ == "__main__":
    sys.exit(main())
