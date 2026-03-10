from dataclasses import dataclass


@dataclass
class IdentityContext:
    actor_name: str = "local_user"
    role: str = "owner"


def current_identity() -> IdentityContext:
    return IdentityContext()
