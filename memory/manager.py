MEMORY_CLASSES = {
    "episodic",
    "semantic",
    "procedural",
    "system",
    "project",
}


def valid_memory_class(value: str) -> bool:
    return value in MEMORY_CLASSES
