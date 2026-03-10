from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class ServiceState:
    name: str
    status: str
    heartbeat_at: datetime


def service_heartbeat(name: str, status: str = "active") -> ServiceState:
    return ServiceState(name=name, status=status, heartbeat_at=datetime.now(timezone.utc))
