"""Workflow loader (YAML -> WorkflowDefinition)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from autoweaver.reactive import StateMachine
import yaml


@dataclass
class WorkflowDefinition:
    """Complete workflow definition parsed from YAML.

    Attributes:
        state_machine: The configured state machine instance.
        task_map: Mapping of state name to task type string.
        side_task_types: List of side task type strings.
    """

    state_machine: StateMachine
    task_map: Dict[str, str] = field(default_factory=dict)
    side_task_types: List[str] = field(default_factory=list)


def load_workflow_from_yaml(path: str) -> WorkflowDefinition:
    """Load a WorkflowDefinition (state machine + task map) from YAML config."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Workflow config not found: {path}")

    data: Dict[str, Any] = yaml.safe_load(config_path.read_text()) or {}
    workflow = data.get("workflow") or {}
    initial = workflow.get("initial") or "idle"
    transitions = workflow.get("transitions") or []

    sm = StateMachine(initial_state=initial, name=workflow.get("name", "workflow"))
    for t in transitions:
        trigger = t.get("trigger")
        source = t.get("source", "*")
        dest = t.get("dest")
        if not trigger or dest is None:
            continue
        sm.add_transition(trigger=trigger, source=source, dest=dest)

    task_map: Dict[str, str] = {}
    for state_name, task_type in (workflow.get("tasks") or {}).items():
        task_map[state_name] = task_type

    side_task_types: List[str] = list(workflow.get("side_tasks") or [])

    return WorkflowDefinition(
        state_machine=sm, task_map=task_map, side_task_types=side_task_types
    )


__all__ = ["WorkflowDefinition", "load_workflow_from_yaml"]
