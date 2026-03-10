from dataclasses import dataclass


@dataclass
class TaskStateTransition:
    task_id: int
    old_state: str
    new_state: str


def transition(task_id: int, old_state: str, new_state: str) -> TaskStateTransition:
    return TaskStateTransition(task_id=task_id, old_state=old_state, new_state=new_state)
