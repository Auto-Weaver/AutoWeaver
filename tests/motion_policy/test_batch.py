from __future__ import annotations

import logging

from autoweaver.motion_policy.batch import (
    Batch,
    BatchResult,
    BatchState,
    ExitReason,
    TeardownOutcome,
)
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.tracer import LogTracer
from autoweaver.motion_policy.world_board import WorldBoard


# ---- Test trees ---------------------------------------------------------

class _ImmediateSuccess(TreeNode):
    def on_start(self) -> Status:
        return Status.SUCCESS

    def on_running(self) -> Status:
        return Status.SUCCESS


class _ImmediateFailure(TreeNode):
    def on_start(self) -> Status:
        return Status.FAILURE

    def on_running(self) -> Status:
        return Status.FAILURE


class _BoomLeaf(TreeNode):
    def on_start(self) -> Status:
        raise ValueError("kaboom")

    def on_running(self) -> Status:
        return Status.RUNNING


class _NeverFinish(TreeNode):
    """Records every tick. Stays RUNNING forever unless halted."""

    def __init__(self):
        super().__init__()
        self.tick_count = 0
        self.snapshots_seen: list = []
        self.halted = False

    def on_start(self) -> Status:
        self.tick_count += 1
        self.snapshots_seen.append(self.snapshot)
        return Status.RUNNING

    def on_running(self) -> Status:
        self.tick_count += 1
        self.snapshots_seen.append(self.snapshot)
        return Status.RUNNING

    def on_halted(self) -> None:
        self.halted = True


class _WriteKey(TreeNode):
    """Writes one blackboard key on its single tick, then SUCCEEDS."""

    def __init__(self, key: str, value, name: str = ""):
        super().__init__(name)
        self._key = key
        self._value = value

    def on_start(self) -> Status:
        self.set_output(self._key, self._value)
        return Status.SUCCESS

    def on_running(self) -> Status:
        return Status.SUCCESS


class _RunNTicks(TreeNode):
    """RUNNING for ``n`` ticks, then SUCCESS. Counts its ticks."""

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


class _RecordingTracer:
    def __init__(self):
        self.events: list[tuple] = []

    def on_batch_begin(self, batch_name):
        self.events.append(("begin", batch_name))

    def on_batch_finish(self, batch_name, result):
        self.events.append(("finish", batch_name, result.success))

    def on_tick_start(self, tick_seq):
        self.events.append(("tick_start", tick_seq))

    def on_tick_end(self, tick_seq, duration, root_status):
        self.events.append(("tick_end", tick_seq, root_status))

    def on_slow_tick(self, duration, target):
        self.events.append(("slow_tick", duration, target))

    def on_node_exception(self, node_name, exception):
        self.events.append(("node_exception", node_name, type(exception).__name__))


# ---- Helpers ------------------------------------------------------------

def _empty_snapshot():
    return WorldBoard().snapshot()


# ---- Ticking / results --------------------------------------------------

def test_tick_returns_success_on_root_success():
    batch = Batch(_ImmediateSuccess)
    assert batch.tick(_empty_snapshot()) == Status.SUCCESS
    assert batch.result is not None
    assert batch.result.success is True
    assert batch.result.reason is ExitReason.COMPLETED
    assert batch.result.final_status == Status.SUCCESS


def test_tick_returns_failure_on_root_failure():
    batch = Batch(_ImmediateFailure)
    assert batch.tick(_empty_snapshot()) == Status.FAILURE
    assert batch.result is not None
    assert batch.result.success is False
    assert batch.result.reason is ExitReason.FAILED


def test_node_exception_recorded_in_batch_result():
    leaf = _BoomLeaf()
    batch = Batch(lambda: leaf)
    batch.tick(_empty_snapshot())
    assert batch.result is not None
    assert batch.result.success is False
    assert batch.result.reason is ExitReason.ERRORED
    assert isinstance(batch.result.exception, ValueError)
    assert batch.result.failed_node == leaf.name


def test_terminal_status_is_idempotent_on_subsequent_ticks():
    """Once SUCCESS/FAILURE is returned, further ticks don't re-tick the tree."""
    counter = {"count": 0}

    class _CountingSuccess(TreeNode):
        def on_start(self) -> Status:
            counter["count"] += 1
            return Status.SUCCESS

        def on_running(self) -> Status:
            counter["count"] += 1
            return Status.SUCCESS

    batch = Batch(_CountingSuccess)
    assert batch.tick(_empty_snapshot()) == Status.SUCCESS
    assert batch.tick(_empty_snapshot()) == Status.SUCCESS
    assert batch.tick(_empty_snapshot()) == Status.SUCCESS
    # Tree was only ticked once.
    assert counter["count"] == 1


def test_snapshot_passed_to_tree_each_tick():
    board = WorldBoard()
    board.declare_state("test.k", int, writer="w")
    board.post_state("test.k", 1, writer="w")

    tree = _NeverFinish()
    batch = Batch(lambda: tree)

    for _ in range(3):
        batch.tick(board.snapshot())

    assert tree.tick_count == 3
    assert all(s["test.k"] == 1 for s in tree.snapshots_seen)


# ---- Lifecycle: READY → RUNNING → EXITED --------------------------------

def test_lifecycle_ready_running_exited():
    tree = _RunNTicks(2)
    batch = Batch(lambda: tree)
    assert batch.state is BatchState.READY
    assert batch.result is None

    batch.tick(_empty_snapshot())
    assert batch.state is BatchState.RUNNING
    assert batch.result is None

    batch.tick(_empty_snapshot())
    assert batch.state is BatchState.EXITED
    assert batch.result is not None


def test_lifecycle_exited_on_kill():
    batch = Batch(_NeverFinish)
    batch.tick(_empty_snapshot())
    assert batch.state is BatchState.RUNNING
    batch.kill()
    assert batch.state is BatchState.EXITED
    assert batch.result is not None
    assert batch.result.reason is ExitReason.KILLED


def test_kill_halts_tree_and_stops_ticking():
    tree = _NeverFinish()
    batch = Batch(lambda: tree)

    batch.tick(_empty_snapshot())
    batch.tick(_empty_snapshot())
    assert tree.tick_count == 2

    batch.kill()
    assert tree.halted
    assert batch.result is not None
    assert batch.result.reason is ExitReason.KILLED
    assert batch.result.message == "killed"

    # Subsequent ticks are no-ops.
    batch.tick(_empty_snapshot())
    assert tree.tick_count == 2  # didn't advance


def test_kill_is_idempotent():
    tree = _NeverFinish()
    batch = Batch(lambda: tree)
    batch.tick(_empty_snapshot())
    batch.kill()
    first = batch.result
    batch.kill()  # should not raise, should not replace the result
    batch.kill()
    assert batch.result is first
    assert batch.state is BatchState.EXITED


def test_kill_before_first_tick_exits_with_a_result():
    """A killed Batch always carries an exit result — even one that never
    got a tick. Unlike the old Action.halt(), which left last_result None."""
    tree = _NeverFinish()
    batch = Batch(lambda: tree)
    batch.kill()
    assert batch.state is BatchState.EXITED
    assert batch.result is not None
    assert batch.result.reason is ExitReason.KILLED
    assert tree.halted is False  # never ran, nothing to halt


def test_kill_after_exit_is_a_noop():
    batch = Batch(_ImmediateSuccess)
    batch.tick(_empty_snapshot())
    result = batch.result
    batch.kill()
    assert batch.result is result
    assert batch.result.reason is ExitReason.COMPLETED


# ---- Teardown tree ------------------------------------------------------

def test_no_teardown_tree_costs_zero_ticks():
    tree = _RunNTicks(1)
    batch = Batch(lambda: tree)
    assert batch.tick(_empty_snapshot()) is Status.SUCCESS
    # Exited on the very tick the main tree finished — no extra tick.
    assert batch.state is BatchState.EXITED


def test_teardown_runs_on_success_path():
    teardown = _RunNTicks(2, name="teardown")
    batch = Batch(_ImmediateSuccess, build_teardown=lambda: teardown)

    batch.tick(_empty_snapshot())  # main tree SUCCESS → enter exit path
    assert batch.state is BatchState.RUNNING
    assert teardown.tick_count == 0

    batch.tick(_empty_snapshot())
    assert batch.state is BatchState.RUNNING  # still RUNNING during teardown
    assert teardown.tick_count == 1

    batch.tick(_empty_snapshot())
    assert teardown.tick_count == 2
    assert batch.state is BatchState.EXITED
    # The exit reason is the *main* tree's, not the teardown tree's.
    assert batch.result.reason is ExitReason.COMPLETED


def test_teardown_runs_on_failure_path():
    teardown = _RunNTicks(1, name="teardown")
    batch = Batch(_ImmediateFailure, build_teardown=lambda: teardown)

    batch.tick(_empty_snapshot())
    assert batch.state is BatchState.RUNNING
    batch.tick(_empty_snapshot())
    assert teardown.tick_count == 1
    assert batch.state is BatchState.EXITED
    assert batch.result.reason is ExitReason.FAILED


def test_teardown_runs_on_kill_path():
    main = _NeverFinish()
    teardown = _RunNTicks(1, name="teardown")
    batch = Batch(lambda: main, build_teardown=lambda: teardown)

    batch.tick(_empty_snapshot())
    batch.kill()
    # Main tree halted immediately; the Batch is still RUNNING though.
    assert main.halted
    assert batch.state is BatchState.RUNNING
    assert batch.result is None

    batch.tick(_empty_snapshot())
    assert teardown.tick_count == 1
    assert batch.state is BatchState.EXITED
    assert batch.result.reason is ExitReason.KILLED
    # The main tree is not ticked again during teardown.
    assert main.tick_count == 1


def test_kill_during_teardown_is_idempotent():
    main = _NeverFinish()
    teardown = _RunNTicks(3, name="teardown")
    batch = Batch(lambda: main, build_teardown=lambda: teardown)

    batch.tick(_empty_snapshot())
    batch.kill()
    batch.tick(_empty_snapshot())
    batch.kill()  # second kill while the teardown tree is running
    batch.kill()
    assert batch.state is BatchState.RUNNING
    assert teardown.tick_count == 1  # kill did not restart or skip teardown

    batch.tick(_empty_snapshot())
    batch.tick(_empty_snapshot())
    assert batch.state is BatchState.EXITED
    assert batch.result.reason is ExitReason.KILLED


def test_teardown_shares_the_batchs_blackboard():
    class _ReadKey(TreeNode):
        def __init__(self):
            super().__init__()
            self.seen = None

        def on_start(self) -> Status:
            self.seen = self.get_input("shared.k")
            return Status.SUCCESS

        def on_running(self) -> Status:
            return Status.SUCCESS

    reader = _ReadKey()
    batch = Batch(
        lambda: _WriteKey("shared.k", 7, name="main_writer"),
        build_teardown=lambda: reader,
    )
    batch.tick(_empty_snapshot())
    batch.tick(_empty_snapshot())
    assert reader.seen == 7


def test_teardown_failure_still_exits(caplog):
    batch = Batch(_ImmediateSuccess, build_teardown=_ImmediateFailure)
    batch.tick(_empty_snapshot())
    with caplog.at_level(logging.WARNING):
        batch.tick(_empty_snapshot())
    assert batch.state is BatchState.EXITED
    # The teardown outcome never rewrites *why* the batch ended.
    assert batch.result.reason is ExitReason.COMPLETED
    assert any("teardown tree returned FAILURE" in r.message for r in caplog.records)


# ---- teardown_outcome: the three cases ---------------------------------

def test_teardown_outcome_none_when_no_teardown_tree():
    batch = Batch(_ImmediateSuccess)
    batch.tick(_empty_snapshot())
    assert batch.result.teardown_outcome is TeardownOutcome.NONE
    assert batch.result.teardown_ok is True


def test_teardown_outcome_succeeded():
    batch = Batch(_ImmediateSuccess, build_teardown=_ImmediateSuccess)
    batch.tick(_empty_snapshot())
    batch.tick(_empty_snapshot())
    assert batch.result.teardown_outcome is TeardownOutcome.SUCCEEDED
    assert batch.result.teardown_ok is True


def test_teardown_outcome_failed():
    """The safety case: a WaitFor(...).timeout() that fired — the device
    may still be moving, and the business must be able to see that."""
    batch = Batch(_ImmediateSuccess, build_teardown=_ImmediateFailure)
    batch.tick(_empty_snapshot())
    batch.tick(_empty_snapshot())
    assert batch.result.teardown_outcome is TeardownOutcome.FAILED
    assert batch.result.teardown_ok is False
    # Still a successful batch — the two questions stay separate.
    assert batch.result.success is True


def test_teardown_outcome_failed_on_the_kill_path_too():
    batch = Batch(_NeverFinish, build_teardown=_ImmediateFailure)
    batch.tick(_empty_snapshot())
    batch.kill()
    batch.tick(_empty_snapshot())
    assert batch.result.reason is ExitReason.KILLED
    assert batch.result.teardown_outcome is TeardownOutcome.FAILED


def test_teardown_outcome_aborted_when_the_tick_machinery_blew_up():
    batch = Batch(_NeverFinish, build_teardown=_ImmediateSuccess)
    batch.tick(_empty_snapshot())
    batch._abort(RuntimeError("boom"))
    assert batch.state is BatchState.EXITED
    assert batch.result.reason is ExitReason.ERRORED
    assert batch.result.teardown_outcome is TeardownOutcome.ABORTED
    assert batch.result.teardown_ok is False


def test_abort_without_an_exception_keeps_the_pending_reason():
    """The shutdown case: nothing went wrong with the Batch itself, so a
    kill in flight stays a kill — only the teardown is marked ABORTED."""
    batch = Batch(_NeverFinish, build_teardown=lambda: _RunNTicks(3))
    batch.tick(_empty_snapshot())
    batch.kill()
    assert batch.result is None  # teardown still pending
    batch._abort(message="clock shut down")

    assert batch.state is BatchState.EXITED
    assert batch.result.reason is ExitReason.KILLED
    assert batch.result.exception is None
    assert batch.result.message == "clock shut down"
    assert batch.result.teardown_outcome is TeardownOutcome.ABORTED


def test_abort_without_an_exception_on_a_batch_that_never_exited():
    """No pending result at all — falls back to KILLED, never to nothing."""
    batch = Batch(_NeverFinish)
    batch.tick(_empty_snapshot())
    batch._abort()
    assert batch.result is not None
    assert batch.result.reason is ExitReason.KILLED


def test_abort_without_a_teardown_tree_reports_none():
    batch = Batch(_NeverFinish)
    batch.tick(_empty_snapshot())
    batch._abort(RuntimeError("boom"))
    assert batch.result.teardown_outcome is TeardownOutcome.NONE
    assert isinstance(batch.result.exception, RuntimeError)


def test_abort_halts_the_trees_and_is_idempotent():
    main = _NeverFinish()
    batch = Batch(lambda: main)
    batch.tick(_empty_snapshot())
    batch._abort(RuntimeError("boom"))
    assert main.halted
    first = batch.result
    batch._abort(RuntimeError("again"))
    assert batch.result is first


def test_abort_exits_even_when_the_tracer_is_broken():
    """Every step of the abort is individually guarded — the Batch must
    reach EXITED even if the tracer is the thing that is broken."""

    class _BrokenTracer:
        def on_batch_begin(self, batch_name):
            pass

        def on_batch_finish(self, batch_name, result):
            raise RuntimeError("tracer is broken")

        def on_tick_start(self, tick_seq):
            pass

        def on_tick_end(self, tick_seq, duration, root_status):
            pass

        def on_slow_tick(self, duration, target):
            pass

        def on_node_exception(self, node_name, exception):
            pass

    batch = Batch(_NeverFinish, tracer=_BrokenTracer())
    batch.tick(_empty_snapshot())
    batch._abort(RuntimeError("boom"))
    assert batch.state is BatchState.EXITED
    assert batch.result is not None


def test_kill_before_first_tick_still_runs_teardown():
    teardown = _RunNTicks(1, name="teardown")
    batch = Batch(_NeverFinish, build_teardown=lambda: teardown)
    batch.kill()
    assert batch.state is BatchState.READY  # never ticked yet
    batch.tick(_empty_snapshot())
    assert teardown.tick_count == 1
    assert batch.state is BatchState.EXITED


# ---- params (in) --------------------------------------------------------

def test_params_land_in_the_blackboard_via_set_initial():
    batch = Batch(_NeverFinish, params={"job.count": 4, "job.recipe": "A"})
    assert batch.blackboard.read("job.count") == 4
    assert batch.blackboard.read("job.recipe") == "A"


def test_params_are_visible_to_the_tree_on_the_first_tick():
    class _ReadParam(TreeNode):
        def __init__(self):
            super().__init__()
            self.seen = None

        def on_start(self) -> Status:
            self.seen = self.get_input("job.count")
            return Status.SUCCESS

        def on_running(self) -> Status:
            return Status.SUCCESS

    leaf = _ReadParam()
    batch = Batch(lambda: leaf, params={"job.count": 9})
    batch.tick(_empty_snapshot())
    assert leaf.seen == 9


def test_params_bypass_writer_checks():
    """set_initial is the front door for outside-the-tree values — no
    node has to own the key first."""
    batch = Batch(_NeverFinish, params={"unowned.key": object()})
    assert batch.blackboard.has_key("unowned.key")


def test_params_are_frozen_at_construction():
    incoming = {"job.count": 1}
    batch = Batch(_NeverFinish, params=incoming)
    incoming["job.count"] = 99
    incoming["job.extra"] = "late"
    assert batch.blackboard.read("job.count") == 1
    assert batch.blackboard.has_key("job.extra") is False
    assert batch.params == {"job.count": 1}


# ---- export (out) -------------------------------------------------------

def test_export_only_declared_keys():
    def build():
        return _WriteKey("out.kept", 1, name="w1") >> _WriteKey(
            "out.dropped", 2, name="w2"
        )

    batch = Batch(build, export=["out.kept"])
    batch.tick(_empty_snapshot())
    batch.tick(_empty_snapshot())
    assert batch.state is BatchState.EXITED
    assert batch.result.exported == {"out.kept": 1}


def test_export_declared_but_unwritten_key_is_absent_not_none():
    batch = Batch(_ImmediateSuccess, export=["out.never_written"])
    batch.tick(_empty_snapshot())
    assert batch.result.exported == {}
    assert "out.never_written" not in batch.result.exported


def test_export_keeps_a_written_none():
    """"never wrote it" and "wrote None" must stay distinguishable."""
    batch = Batch(lambda: _WriteKey("out.k", None), export=["out.k"])
    batch.tick(_empty_snapshot())
    assert batch.result.exported == {"out.k": None}
    assert "out.k" in batch.result.exported


def test_export_defaults_to_nothing():
    batch = Batch(lambda: _WriteKey("out.k", 1))
    batch.tick(_empty_snapshot())
    assert batch.result.exported == {}


def test_export_collected_on_the_kill_path_too():
    def build():
        return _WriteKey("out.k", 5, name="w") >> _NeverFinish()

    batch = Batch(build, export=["out.k"])
    batch.tick(_empty_snapshot())
    batch.kill()
    assert batch.result.exported == {"out.k": 5}


def test_teardown_writes_are_exported():
    batch = Batch(
        _ImmediateSuccess,
        build_teardown=lambda: _WriteKey("out.parked", True, name="parker"),
        export=["out.parked"],
    )
    batch.tick(_empty_snapshot())
    batch.tick(_empty_snapshot())
    assert batch.result.exported == {"out.parked": True}


def test_framework_does_not_chain_exports_into_the_next_batch():
    """No auto-carry: the business merges exported into the next params."""
    first = Batch(lambda: _WriteKey("out.k", 3), export=["out.k"])
    first.tick(_empty_snapshot())
    carry = first.result.exported

    second = Batch(_NeverFinish)
    assert second.blackboard.has_key("out.k") is False

    third = Batch(_NeverFinish, params=carry)
    assert third.blackboard.read("out.k") == 3


# ---- identity -----------------------------------------------------------

def test_batch_ids_are_framework_assigned_and_unique():
    a = Batch(_ImmediateSuccess)
    b = Batch(_ImmediateSuccess)
    assert a.id != b.id
    assert a.batch_no is None


def test_batch_no_is_a_field_not_a_key():
    """Same tray re-run: one batch_no, two Batch objects."""
    a = Batch(_ImmediateSuccess, batch_no="MES-42")
    b = Batch(_ImmediateSuccess, batch_no="MES-42")
    assert a.batch_no == b.batch_no == "MES-42"
    assert a.id != b.id
    assert a.info.batch_no == "MES-42"
    assert a.info.id == a.id


def test_a_batch_runs_once_but_the_factory_is_reusable():
    """The program is the factory; each Batch gets a fresh tree."""
    built: list[TreeNode] = []

    def build():
        tree = _ImmediateSuccess()
        built.append(tree)
        return tree

    first = Batch(build)
    first.tick(_empty_snapshot())
    assert first.state is BatchState.EXITED

    second = Batch(build)
    assert second.state is BatchState.READY
    second.tick(_empty_snapshot())
    assert second.state is BatchState.EXITED

    assert len(built) == 2
    assert built[0] is not built[1]


def test_name_defaults_to_tree_name():
    batch = Batch(_ImmediateSuccess)
    assert batch.name == "_ImmediateSuccess"
    named = Batch(_ImmediateSuccess, name="job")
    assert named.name == "job"


# ---- Tracer -------------------------------------------------------------

def test_tracer_lifecycle_events_on_success():
    tracer = _RecordingTracer()
    batch = Batch(_ImmediateSuccess, tracer=tracer)
    batch.tick(_empty_snapshot())
    kinds = [e[0] for e in tracer.events]
    assert kinds[0] == "begin"
    assert "tick_start" in kinds
    assert "tick_end" in kinds
    assert kinds[-1] == "finish"


def test_tracer_finish_fires_after_teardown_not_before():
    tracer = _RecordingTracer()
    batch = Batch(
        _ImmediateSuccess, build_teardown=lambda: _RunNTicks(2), tracer=tracer
    )
    batch.tick(_empty_snapshot())
    assert [e for e in tracer.events if e[0] == "finish"] == []
    batch.tick(_empty_snapshot())
    batch.tick(_empty_snapshot())
    assert [e[0] for e in tracer.events][-1] == "finish"


def test_slow_tick_warning_emitted(caplog):
    """A tree that sleeps in on_start triggers a slow tick warning."""
    import time

    class _SlowLeaf(TreeNode):
        def on_start(self):
            time.sleep(0.05)
            return Status.SUCCESS

        def on_running(self):
            return Status.SUCCESS

    tracer = _RecordingTracer()
    # Tight budget: anything over 10 ms is "slow".
    batch = Batch(_SlowLeaf, tracer=tracer, slow_tick_budget_s=0.01)
    with caplog.at_level(logging.WARNING):
        batch.tick(_empty_snapshot())
    slow_events = [e for e in tracer.events if e[0] == "slow_tick"]
    assert len(slow_events) == 1
    assert any("slow tick" in r.message for r in caplog.records)


def test_node_exception_event_fires_on_tracer():
    tracer = _RecordingTracer()
    batch = Batch(_BoomLeaf, tracer=tracer)
    batch.tick(_empty_snapshot())
    exc_events = [e for e in tracer.events if e[0] == "node_exception"]
    assert len(exc_events) == 1
    assert exc_events[0][2] == "ValueError"


def test_log_tracer_does_not_blow_up():
    batch = Batch(_ImmediateSuccess, tracer=LogTracer())
    status = batch.tick(_empty_snapshot())
    assert status == Status.SUCCESS


# ---- BatchResult --------------------------------------------------------

def test_result_success_is_derived_from_reason():
    assert BatchResult(reason=ExitReason.COMPLETED).success is True
    assert BatchResult(reason=ExitReason.FAILED).success is False
    assert BatchResult(reason=ExitReason.ERRORED).success is False
    assert BatchResult(reason=ExitReason.KILLED).success is False


def test_result_teardown_ok_is_derived_from_outcome():
    def r(outcome):
        return BatchResult(reason=ExitReason.COMPLETED, teardown_outcome=outcome)

    assert r(TeardownOutcome.NONE).teardown_ok is True
    assert r(TeardownOutcome.SUCCEEDED).teardown_ok is True
    assert r(TeardownOutcome.FAILED).teardown_ok is False
    assert r(TeardownOutcome.ABORTED).teardown_ok is False


def test_success_and_teardown_ok_are_independent():
    """A batch can succeed with a broken cleanup, or fail with a clean one."""
    assert BatchResult(
        reason=ExitReason.COMPLETED, teardown_outcome=TeardownOutcome.FAILED
    ).success is True
    assert BatchResult(
        reason=ExitReason.FAILED, teardown_outcome=TeardownOutcome.SUCCEEDED
    ).teardown_ok is True
