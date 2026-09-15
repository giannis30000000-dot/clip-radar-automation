"""Bounded Cloudinary bridge cleanup for the PC-independent Action runner."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from cloudinary_media_host import CloudinaryMediaHost, write_cleanup_summary
from publication_state import PublicationLedger


def main() -> int:
    state_path = Path(os.getenv("CLIP_RADAR_PUBLICATION_STATE_FILE", "state/publications.json"))
    output_path = Path(os.getenv("CLIP_RADAR_OUTPUT_DIR", "output")) / "cloudinary_cleanup_summary.json"
    ledger = PublicationLedger(state_path)
    host = CloudinaryMediaHost()
    summary = host.cleanup_expired(ledger)
    write_cleanup_summary(output_path, summary)
    print(
        f"cloudinary cleanup | status={summary['status']} | candidates={summary['candidates']} | "
        f"processed={summary['processed']} | api_calls={summary['api_calls']}"
    )
    return 0 if summary["status"] == "READY" else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
