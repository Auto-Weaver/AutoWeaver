"""BTClock's Batch surface — submit / kill / reap / on_batch_start. EVO-014.

The four verbs are create, submit, get the result, kill. Nothing here says
*when* or *whether* to run a Batch: that is business policy, and it lives
in the business's own tick loop.
"""

from __future__ import annotations

import pytest

from autoweaver.motion_policy.batch import (
    Batch,
    BatchInfo,
    BatchState,
    ExitReason,
    TeardownOutcome,
)
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.world_board import WorldBoard
from autoweaver.worker.base import WorkerState
from autoweaver.worker.clock import BTClock
from autoweaver.worker.perception import PerceptionWorker


# ---- Trees --------------------------------------------------------------

class _ImmediateSuccess(TreeNode):
    def on_start(self) -> Status:
        return Status.SUCCESS

    def on_running(self) -> Status:
        return Status.SUCCESS


class _Forever(TreeNode):
    def __init__(self, name: str = ""):
        super().__init__(name)
        self.tick_count = 0
        self.halted = False

    def on_start(self) -> Status:
        self.tick_count += 1
        return Status.RUNNING

    def on_running(self) -> Status:
        self.tick_count += 1
        return Status.RUNNING

    def on_halted(self) -> None:
        self.halted = True


class _RunNTicks(TreeNode):
    def __init__(self, n: int, name: str = ""):
        super().__init__(name)
        self._n = n
        self.tick_count = 0

    def on_start(self) -> Status:
        return self._advance()

    def on_running(self) -> Status:
        return self._advance()

    def _advance(self) -> Status:
        self.tick_count += 1
        return Status.SUCCESS if self.tick_count >= self._n else Status.RUNNING


# ---- Workers ------------------------------------------------------------

class _BatchAwareWorker(PerceptionWorker):
    def __init__(self, name: str = "aware", raise_on_batch_start: bool = False):
        super().__init__()
        self._n = name
        self.batches_seen: list[BatchInfo] = []
        self.tick_count = 0
        self._raise = raise_on_batch_start
        self.stopped = False

    @property
    def name(self) -> str:
        return self._n

    def on_batch_start(self, info: BatchInfo) -> None:
        if self._raise:
            raise RuntimeError("cannot reset")
        self.batches_seen.append(info)

    def on_tick(self, ctx) -> None:
        self.tick_count += 1

    def on_stop(self) -> None:
        self.stopped = True


class _PlainWorker(PerceptionWorker):
    """Does not override on_batch_start — the default must be a no-op."""

    def __init__(self, name: str = "plain"):
        super().__init__()
        self._n = name

    @property
    def name(self) -> str:
        return self._n


# ---- Fixtures -----------------------------------------------------------

@pytest.fixture
def clock():
    c = BTClock(world_board=WorldBoard())
    yield c
    c.shutdown()


# ---- One Batch at a time ------------------------------------------------

def test_second_submit_is_rejected_while_one_runs(clock):
    first = Batch(_Forever, name="first")
    clock.submit(first)
    with pytest.raises(RuntimeError, match="already running"):
        clock.submit(Batch(_Forever, name="second"))
    assert clock.attached_batches() == ["first"]


def test_the_limit_is_a_policy_check_not_a_singular_slot(clock):
    """The structure stays plural so lifting the limit is deleting a check."""
    assert isinstance(clock._batches, list)


def test_next_batch_can_be_submitted_after_the_first_exits(clock):
    """The whole point of replacing Action: the clock re-arms."""
    first = Batch(_ImmediateSuccess, name="first")
    clock.submit(first)
    clock.tick_once()
    assert first.state is BatchState.EXITED
    assert clock.attached_batches() == []  # reaped

    tree = _RunNTicks(1)
    second = Batch(lambda: tree, name="second")
    clock.submit(second)
    clock.tick_once()
    assert second.state is BatchState.EXITED
    assert tree.tick_count == 1


def test_next_batch_can_be_submitted_after_a_kill(clock):
    first = Batch(_Forever, name="first")
    handle = clock.submit(first)
    clock.tick_once()
    clock.kill(handle)
    assert clock.attached_batches() == []

    second = Batch(_ImmediateSuccess, name="second")
    clock.submit(second)
    clock.tick_once()
    assert second.result.success is True


def test_resubmitting_an_exited_batch_is_rejected(clock):
    batch = Batch(_ImmediateSuccess)
    clock.submit(batch)
    clock.tick_once()
    with pytest.raises(RuntimeError, match="a Batch runs once"):
        clock.submit(batch)


def test_running_batch_cannot_be_submitted_to_a_second_clock(clock):
    batch = Batch(_Forever)
    clock.submit(batch)
    clock.tick_once()
    other = BTClock(world_board=WorldBoard())
    try:
        with pytest.raises(RuntimeError, match="a Batch runs once"):
            other.submit(batch)
    finally:
        other.shutdown()


# ---- Handle: state / result --------------------------------------------

def test_handle_exposes_state_and_result(clock):
    handle = clock.submit(Batch(lambda: _RunNTicks(2)))
    assert handle.state is BatchState.READY
    assert handle.result is None

    clock.tick_once()
    assert handle.state is BatchState.RUNNING
    assert handle.result is None

    clock.tick_once()
    assert handle.state is BatchState.EXITED
    assert handle.result is not None
    assert handle.result.reason is ExitReason.COMPLETED


def test_no_blocking_wait_on_the_clock(clock):
    """The business owns the tick loop; the framework offers no wait()."""
    assert not hasattr(clock, "wait")
    handle = clock.submit(Batch(_ImmediateSuccess))
    assert not hasattr(handle, "wait")


# ---- kill ---------------------------------------------------------------

def test_kill_is_idempotent_at_the_clock(clock):
    tree = _Forever()
    handle = clock.submit(Batch(lambda: tree))
    clock.tick_once()
    clock.kill(handle)
    result = handle.result
    clock.kill(handle)  # already reaped — must be a no-op
    clock.kill(handle)
    assert handle.result is result
    assert tree.halted


def test_kill_runs_the_teardown_tree_before_reaping(clock):
    main = _Forever()
    teardown = _RunNTicks(2, name="teardown")
    handle = clock.submit(Batch(lambda: main, build_teardown=lambda: teardown))

    clock.tick_once()
    clock.kill(handle)
    # Still attached: the teardown tree needs ticks.
    assert clock.attached_batches() != []
    assert handle.state is BatchState.RUNNING

    clock.tick_once()
    clock.tick_once()
    assert teardown.tick_count == 2
    assert handle.state is BatchState.EXITED
    assert clock.attached_batches() == []
    assert handle.result.reason is ExitReason.KILLED
    # A second Batch can go in right away.
    clock.submit(Batch(_ImmediateSuccess))


def test_kill_during_teardown_does_not_cut_it_short(clock):
    main = _Forever()
    teardown = _RunNTicks(2, name="teardown")
    handle = clock.submit(Batch(lambda: main, build_teardown=lambda: teardown))
    clock.tick_once()
    clock.kill(handle)
    clock.tick_once()
    clock.kill(handle)  # mid-teardown kill: idempotent, not a second exit
    assert handle.state is BatchState.RUNNING
    clock.tick_once()
    assert handle.state is BatchState.EXITED


def test_teardown_runs_under_the_clock_on_the_success_path(clock):
    teardown = _RunNTicks(1, name="teardown")
    handle = clock.submit(
        Batch(_ImmediateSuccess, build_teardown=lambda: teardown)
    )
    clock.tick_once()
    assert handle.state is BatchState.RUNNING
    clock.tick_once()
    assert teardown.tick_count == 1
    assert handle.state is BatchState.EXITED
    assert handle.result.reason is ExitReason.COMPLETED


def test_shutdown_kills_the_running_batch(clock):
    tree = _Forever()
    handle = clock.submit(Batch(lambda: tree))
    clock.tick_once()
    clock.shutdown()
    assert tree.halted
    assert handle.state is BatchState.EXITED
    assert handle.result.reason is ExitReason.KILLED
    assert clock.attached_batches() == []


def test_shutdown_warns_when_a_teardown_tree_cannot_run(clock, caplog):
    import logging

    handle = clock.submit(
        Batch(_Forever, build_teardown=lambda: _RunNTicks(3), name="wip")
    )
    clock.tick_once()
    with caplog.at_level(logging.WARNING):
        clock.shutdown()
    assert any("teardown tree cannot run" in r.message for r in caplog.records)
    assert clock.attached_batches() == []


def test_shutdown_still_produces_a_result(clock):
    """``result is not None`` ⇔ finished must hold on the shutdown path too.

    Otherwise the loop we document everywhere —
    ``while handle.result is None: clock.tick_once()`` — hangs forever
    after a shutdown, on the one path where hanging is least acceptable.
    """
    tree = _Forever()
    handle = clock.submit(
        Batch(lambda: tree, build_teardown=lambda: _RunNTicks(3), name="wip")
    )
    clock.tick_once()
    clock.shutdown()

    assert handle.state is BatchState.EXITED
    assert handle.result is not None
    # A result here is not evidence the cleanup ran — it is evidence of
    # the opposite.
    assert handle.result.teardown_outcome is TeardownOutcome.ABORTED
    assert handle.result.teardown_ok is False
    # The kill in flight stays a kill; shutdown is not a new reason.
    assert handle.result.reason is ExitReason.KILLED
    assert tree.halted


def test_shutdown_without_a_teardown_tree_reports_none(clock):
    handle = clock.submit(Batch(_Forever))
    clock.tick_once()
    clock.shutdown()
    assert handle.result is not None
    assert handle.result.reason is ExitReason.KILLED
    assert handle.result.teardown_outcome is TeardownOutcome.NONE
    assert handle.result.teardown_ok is True


def test_shutdown_leaves_an_already_exited_batch_alone(clock):
    handle = clock.submit(Batch(_ImmediateSuccess))
    clock.tick_once()
    result = handle.result
    clock.shutdown()
    assert handle.result is result
    assert handle.result.reason is ExitReason.COMPLETED
    assert handle.result.teardown_outcome is TeardownOutcome.NONE


# ---- handle identity ----------------------------------------------------

def test_handles_compare_by_identity_not_by_fields(clock):
    """A handle is an identity. Two handles onto the same Batch with the
    same name are still two different handles — the clock's ``in`` /
    ``remove`` must never confuse them."""
    from autoweaver.worker.clock import BatchHandle

    batch = Batch(_Forever, name="same")
    real = clock.submit(batch)
    look_alike = BatchHandle(name="same", batch=batch)

    assert real != look_alike
    assert real == real
    assert look_alike not in clock._batches
    assert real in clock._batches

    # Killing the look-alike must not touch the real one.
    clock.kill(look_alike)
    assert real.state is not BatchState.EXITED
    assert clock.attached_batches() == ["same"]

    clock.kill(real)
    assert real.state is BatchState.EXITED


# ---- on_batch_start broadcast ------------------------------------------

def test_on_batch_start_broadcast_to_all_workers(clock):
    a = _BatchAwareWorker("a")
    b = _BatchAwareWorker("b")
    clock.attach_worker(a)
    clock.attach_worker(b)

    batch = Batch(_ImmediateSuccess, batch_no="MES-7")
    clock.submit(batch)

    for worker in (a, b):
        assert len(worker.batches_seen) == 1
        assert worker.batches_seen[0].id == batch.id
        assert worker.batches_seen[0].batch_no == "MES-7"


def test_on_batch_start_fires_once_per_batch(clock):
    worker = _BatchAwareWorker()
    clock.attach_worker(worker)

    clock.submit(Batch(_ImmediateSuccess))
    for _ in range(3):
        clock.tick_once()
    assert len(worker.batches_seen) == 1

    clock.submit(Batch(_ImmediateSuccess))
    clock.tick_once()
    assert len(worker.batches_seen) == 2
    assert worker.batches_seen[0].id != worker.batches_seen[1].id


def test_on_batch_start_carries_identity_only(clock):
    """No params reach the Worker — those belong to the tree."""
    worker = _BatchAwareWorker()
    clock.attach_worker(worker)
    clock.submit(Batch(_ImmediateSuccess, params={"job.secret": 1}))
    info = worker.batches_seen[0]
    assert set(vars(info)) == {"id", "batch_no"}


def test_on_batch_start_default_is_a_noop(clock):
    clock.attach_worker(_PlainWorker())
    clock.submit(Batch(_ImmediateSuccess))
    clock.tick_once()  # no exception is the assertion


def test_on_batch_start_raising_faults_the_worker_and_fails_the_submit(clock):
    good = _BatchAwareWorker("good")
    bad = _BatchAwareWorker("bad", raise_on_batch_start=True)
    clock.attach_worker(good)
    clock.attach_worker(bad)

    batch = Batch(_ImmediateSuccess)
    with pytest.raises(RuntimeError, match="cannot reset"):
        clock.submit(batch)

    assert bad.lifecycle_state is WorkerState.FAULTED
    # The Batch never started, and the slot is free for another attempt.
    assert batch.state is BatchState.READY
    assert clock.attached_batches() == []
    clock.submit(Batch(_ImmediateSuccess))


def test_paused_worker_still_gets_on_batch_start(clock):
    """on_batch_start is a notification, not a tick.

    "Paused" means "do not advance"; it does not mean "keep last batch's
    state". A PAUSED Worker that missed the reset would resume carrying
    exactly the leak this hook exists to prevent.
    """
    worker = _BatchAwareWorker("paused_one")
    clock.attach_worker(worker)
    clock.pause_worker(worker)
    assert worker.lifecycle_state is WorkerState.PAUSED

    batch = Batch(_ImmediateSuccess, batch_no="MES-9")
    clock.submit(batch)

    assert len(worker.batches_seen) == 1
    assert worker.batches_seen[0].id == batch.id
    # ...and it is still paused: the notification does not resume it.
    assert worker.lifecycle_state is WorkerState.PAUSED


def test_paused_worker_still_skips_on_tick(clock):
    """The contrast that makes the rule above coherent."""
    worker = _BatchAwareWorker("paused_two")
    clock.attach_worker(worker)
    clock.pause_worker(worker)
    clock.submit(Batch(_Forever))
    clock.tick_once()
    assert worker.batches_seen != []  # got the batch notification
    assert worker.tick_count == 0     # but no ticks


def test_faulted_worker_is_skipped_by_later_broadcasts(clock):
    bad = _BatchAwareWorker("bad", raise_on_batch_start=True)
    clock.attach_worker(bad)
    with pytest.raises(RuntimeError):
        clock.submit(Batch(_ImmediateSuccess))

    bad._raise = False
    clock.submit(Batch(_ImmediateSuccess))  # FAULTED → not RUNNING → skipped
    assert bad.batches_seen == []


def test_on_batch_start_fires_before_the_first_tick(clock):
    order: list[str] = []

    class _OrderWorker(PerceptionWorker):
        name = "order"

        def on_batch_start(self, info):
            order.append("batch_start")

        def on_tick(self, ctx):
            order.append("tick")

    class _OrderTree(TreeNode):
        def on_start(self):
            order.append("tree_tick")
            return Status.RUNNING

        def on_running(self):
            return Status.RUNNING

    clock.attach_worker(_OrderWorker())
    clock.submit(Batch(_OrderTree))
    clock.tick_once()
    assert order == ["batch_start", "tree_tick", "tick"]


# ---- a raising tick must not wedge the exclusive slot -------------------

class _NotAnException(BaseException):
    """A BaseException — the kind TreeNode.tick does NOT catch."""


class _TickBomb(TreeNode):
    """Blows up inside the tick, past every guard the tree has."""

    def __init__(self, exc: BaseException | None = None):
        super().__init__()
        self._exc = exc or _NotAnException("tick bomb")

    def on_start(self) -> Status:
        raise self._exc

    def on_running(self) -> Status:
        raise self._exc


def test_raising_tick_forces_exit_and_frees_the_slot(clock):
    """The regression that single-Batch made system-level.

    A broken Batch that stayed attached would not merely break itself —
    it would stop the machine from ever accepting another batch.
    """
    handle = clock.submit(Batch(_TickBomb, name="bomb"))
    clock.tick_once()

    assert handle.state is BatchState.EXITED
    assert handle.result.reason is ExitReason.ERRORED
    assert isinstance(handle.result.exception, _NotAnException)
    assert clock.attached_batches() == []

    # ...and the machine keeps working.
    nxt = Batch(_ImmediateSuccess, name="next")
    clock.submit(nxt)
    clock.tick_once()
    assert nxt.result.success is True


def test_raising_tick_does_not_stop_the_tick_loop(clock):
    """Workers still get their on_tick on the same tick the batch blew up."""
    worker = _BatchAwareWorker("w")
    clock.attach_worker(worker)
    clock.submit(Batch(_TickBomb))
    clock.tick_once()
    assert worker.tick_count == 1
    assert clock.attached_batches() == []


def test_raising_teardown_tick_also_frees_the_slot(clock):
    handle = clock.submit(Batch(_Forever, build_teardown=_TickBomb))
    clock.tick_once()
    clock.kill(handle)
    clock.tick_once()  # teardown tree explodes
    assert handle.state is BatchState.EXITED
    assert handle.result.reason is ExitReason.ERRORED
    assert handle.result.teardown_outcome is TeardownOutcome.ABORTED
    assert clock.attached_batches() == []
    clock.submit(Batch(_ImmediateSuccess))


def test_keyboard_interrupt_propagates_but_frees_the_slot_first(clock):
    """KeyboardInterrupt / SystemExit are not errors — Python keeps them
    off Exception so they propagate. They still must not leave a wedged
    slot behind."""
    handle = clock.submit(Batch(lambda: _TickBomb(KeyboardInterrupt())))
    with pytest.raises(KeyboardInterrupt):
        clock.tick_once()

    assert handle.state is BatchState.EXITED
    assert handle.result.reason is ExitReason.ERRORED
    assert clock.attached_batches() == []
    clock.submit(Batch(_ImmediateSuccess))


def test_system_exit_propagates_but_frees_the_slot_first(clock):
    handle = clock.submit(Batch(lambda: _TickBomb(SystemExit(1))))
    with pytest.raises(SystemExit):
        clock.tick_once()
    assert handle.state is BatchState.EXITED
    assert clock.attached_batches() == []


# ---- params / export end-to-end through the clock -----------------------

def test_params_in_exports_out_through_the_clock(clock):
    class _Doubler(TreeNode):
        def on_start(self) -> Status:
            self.set_output("out.doubled", self.get_input("in.n") * 2)
            return Status.SUCCESS

        def on_running(self) -> Status:
            return Status.SUCCESS

    handle = clock.submit(
        Batch(_Doubler, params={"in.n": 21}, export=["out.doubled", "out.missing"])
    )
    clock.tick_once()
    assert handle.result.exported == {"out.doubled": 42}

    # Chaining is business code — one line, written here, not in the clock.
    carry = handle.result.exported
    second = Batch(_Doubler, params={"in.n": carry["out.doubled"]},
                   export=["out.doubled"])
    clock.submit(second)
    clock.tick_once()
    assert second.result.exported == {"out.doubled": 84}
