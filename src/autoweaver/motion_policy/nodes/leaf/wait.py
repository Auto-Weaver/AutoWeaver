from autoweaver.motion_policy.nodes.node import Status, TreeNode


class Wait(TreeNode):
    """Wait for a specified duration. Returns RUNNING until time elapsed.

    Duration is measured in *tick time* (``self.now``, i.e. the TickContext
    timestamp), not wall clock — the deadline is evaluated against the tick
    that observes it, so it lines up with the tick_ids the logbook records
    and is injectable in tests without sleeping.
    """

    def __init__(self, seconds: float, name: str = ""):
        super().__init__(name=name)
        self.seconds = seconds
        self._start_time: float | None = None

    def on_start(self) -> Status:
        self._start_time = self.now
        if self.seconds <= 0:
            return Status.SUCCESS
        return Status.RUNNING

    def on_running(self) -> Status:
        if self.now - self._start_time >= self.seconds:
            self._start_time = None
            return Status.SUCCESS
        return Status.RUNNING

    def on_halted(self) -> None:
        self._start_time = None

    def reset(self) -> None:
        self._start_time = None
        super().reset()
