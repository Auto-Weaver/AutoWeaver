"""Workflow engine — lifecycle manager for state-driven task orchestration."""

import logging
import signal
import threading
from typing import Dict, Optional, Sequence, Set

from autoweaver.reactive import EventBus, StateMachine
from autoweaver.tasks import SideTask, Task

logger = logging.getLogger(__name__)


class WorkflowEngine:
    """Lifecycle manager for state-driven workflows.

    Manages:
    - EventBus creation and ownership
    - StateMachine attachment
    - Task lifecycle (attach/close/reset on state transitions)
    - SideTask lifecycle (attach/close)
    - Graceful shutdown via signals or terminal states

    Does NOT manage:
    - Input sources (camera, sensors, queues)
    - Output sinks (storage, streams, displays)
    - Frame processing or rendering

    Example:
        >>> engine = WorkflowEngine(
        ...     state_machine=sm,
        ...     task_map=tasks,
        ...     side_tasks=[comm_task, frame_loop],
        ... )
        >>> engine.loop()  # Blocks until terminal state or stop()
    """

    def __init__(
        self,
        state_machine: StateMachine,
        task_map: Dict[str, Task],
        side_tasks: Sequence[SideTask] = (),
        event_bus: Optional[EventBus] = None,
        terminal_states: Optional[Set[str]] = None,
        register_signals: bool = True,
    ):
        self.event_bus = event_bus or EventBus()
        self.state_machine = state_machine
        self._task_map = task_map
        self._side_tasks = list(side_tasks)
        self._terminal_states = terminal_states or set()
        self._done = threading.Event()
        self._current_task: Optional[Task] = None

        # Register signal handlers
        if register_signals and threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)

    def loop(self) -> None:
        """Start the workflow and block until completion.

        Sets up all participants (state machine, tasks, side tasks),
        then waits for events to drive state transitions until a
        terminal state is reached or stop() is called.
        """
        logger.info("WorkflowEngine starting...")
        self._setup()
        logger.info("WorkflowEngine running, waiting for events...")
        self._done.wait()
        self._cleanup()
        logger.info("WorkflowEngine stopped.")

    def stop(self) -> None:
        """Signal the engine to stop."""
        logger.info("WorkflowEngine stop requested.")
        self._done.set()

    def _setup(self) -> None:
        """Mount all participants onto the EventBus."""
        # Attach state machine
        self.state_machine.attach(self.event_bus)

        # Subscribe to state changes for task switching
        self.event_bus.subscribe("STATE:CHANGED", self._on_state_changed)

        # Resolve and attach initial task
        initial_state = self.state_machine.get_state()
        self._current_task = self._task_map.get(initial_state)
        if self._current_task is not None:
            self._current_task.attach(self.event_bus)

        # Attach all side tasks
        for st in self._side_tasks:
            st.attach(self.event_bus)

        self.event_bus.publish("SYS:STARTED", {
            "source": "engine",
            "payload": {"message": "workflow started"},
        })

    def _cleanup(self) -> None:
        """Close all participants."""
        logger.info("Cleaning up WorkflowEngine...")

        # Close side tasks
        for st in self._side_tasks:
            try:
                st.close()
            except Exception as e:
                logger.warning("Error closing side task %s: %s", st.name, e)

        # Close all tasks
        for t in self._task_map.values():
            try:
                t.close()
            except Exception as e:
                logger.warning("Error closing task %s: %s", t.name, e)

        self.event_bus.publish("SYS:STOPPED", {
            "source": "engine",
            "payload": {"message": "workflow stopped"},
        })

    def _on_state_changed(self, event: str, data: dict) -> None:
        """Handle STATE:CHANGED — switch the active task."""
        payload = data.get("payload", {})
        new_state = payload.get("new_state")
        old_state = payload.get("old_state")

        # Check for terminal state
        if new_state in self._terminal_states:
            logger.info("Reached terminal state '%s', stopping.", new_state)
            self.stop()
            return

        new_task = self._task_map.get(new_state)

        # Close old task
        if self._current_task is not None:
            self._current_task.close()

        # Switch to new task
        self._current_task = new_task
        if self._current_task is not None:
            self._current_task.attach(self.event_bus)
            self._current_task.reset()

        logger.info(
            "Task switched: state %s → %s, task: %s",
            old_state,
            new_state,
            self._current_task.name if self._current_task else "None (idle)",
        )

    def _signal_handler(self, signum, frame) -> None:
        signal_name = signal.Signals(signum).name
        logger.info("Received %s, initiating graceful shutdown...", signal_name)
        self.stop()
