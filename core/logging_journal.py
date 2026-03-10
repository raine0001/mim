import json
import logging
from datetime import datetime, timezone
from typing import Any


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def journal_event(actor: str, action: str, result: str, metadata: dict[str, Any] | None = None) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor,
        "action": action,
        "result": result,
        "metadata": metadata or {},
    }
    logging.getLogger("mim.journal").info(json.dumps(payload))
