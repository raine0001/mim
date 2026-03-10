from dataclasses import dataclass


@dataclass
class ToolSpec:
    name: str
    description: str
    enabled: bool = True
