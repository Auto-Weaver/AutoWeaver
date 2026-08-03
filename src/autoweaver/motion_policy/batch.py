"""Batch — the framework's execution unit. See EVO-014.

A ``Batch`` is AutoWeaver's *process*: it has an identity, a private
address space (its ``Blackboard``), a lifecycle, and an exit result.
It replaces the old ``Action``, which was a Future in disguise — a
one-shot container that latched ``_finished`` and could never be
re-armed.

The program a Batch runs is **not a tree object, it is the function that
builds one** (EVO-014 §4): tree nodes carry mutable state, so a tree
instance is per-run material. ``Batch`` takes a factory and calls it
once, at construction.
"""

from __future__ import annotations

import itertools
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Iterable

from autoweaver.motion_policy.blackboard import Blackboard
from autoweaver.motion_policy.nodes.node import Status, TreeNode
from autoweaver.motion_policy.tracer import BatchTracer, NullTracer
from autoweaver.motion_policy.world_board import Snapshot

if TYPE_CHECKING:
    from autoweaver.worker.base import TickContext


logger = logging.getLogger(__name__)

#: A "program": a zero-argument function returning a fresh tree.
TreeFactory = Callable[[], TreeNode]

# Process-wide monotonic counter for framework-assigned Batch ids. Starts
# at 1 so 0 can safely mean "no batch". Mirrors ``next_request_id``.
_batch_id_counter = itertools.count(1)


def next_batch_id() -> int:
    """Allocate the next framework-assigned Batch id."""
    return next(_batch_id_counter)


class BatchState(Enum):
    """One straight line, no branches, no way back (EVO-014 §7).

    ``RUNNING`` covers the teardown tree too — teardown is code on the
    exit path, not a state. There is deliberately no ``PAUSED``, no
    ``FAULTED`` and no ``EXITING``.
    """

    READY = "ready"
    RUNNING = "running"
    EXITED = "exited"


class ExitReason(Enum):
    """*Why* a Batch ended — data, not state (EVO-014 §7).

    ``FAILED`` and ``ERRORED`` are both "the tree returned FAILURE"; they
    differ in whether a node raised on the way (``ERRORED`` always
    carries a non-None ``BatchResult.exception``).
    """

    COMPLETED = "completed"  # main tree returned SUCCESS
    FAILED = "failed"        # main tree returned FAILURE
    ERRORED = "errored"      # a node raised (or the tick itself did)
    KILLED = "killed"        # kill() was called


class TeardownOutcome(Enum):
    """How the teardown tree ended — safety information, not a state.

    It never overwrites ``BatchResult.reason``: *why the batch ended* and
    *whether the cleanup worked* are two different questions.

    ``FAILED`` is the one that matters operationally. The canonical
    teardown tree is ``WaitFor("arm.running", is False).timeout(5.0)`` —
    so ``FAILED`` typically means **the timeout fired and the device may
    still be moving**. A business loop should treat it as a reason not to
    submit the next Batch.
    """

    NONE = "none"            # no teardown tree was given — nothing to run
    SUCCEEDED = "succeeded"  # teardown tree returned SUCCESS
    FAILED = "failed"        # teardown tree returned FAILURE (e.g. timeout)
    ABORTED = "aborted"      # teardown never finished (the tick raised)


@dataclass(frozen=True)
class BatchInfo:
    """The minimal batch identity handed to ``Worker.on_batch_start``.

    Deliberately *only* identity: ``params`` are for the tree, not for
    Workers (EVO-014 §10). A Worker that needs parameters is being
    configured, and configuration arrives at ``on_attach``.

    ``batch_no`` is the shop-floor / MES number. It is a **field, not a
    key**: it may be absent, and re-running the same tray of parts gives
    one ``batch_no`` and two ``Batch`` objects with distinct ``id``s.
    """

    id: int
    batch_no: str | None = None


@dataclass
class BatchResult:
    """A Batch's exit result: framework part + business part.

    Framework part — ``reason`` / ``message`` / ``exception`` /
    ``failed_node`` / ``final_status`` / ``teardown_outcome``.

    Business part — ``exported``: the blackboard keys the business asked
    for at submit time. **Keys that were declared but never written are
    absent from the dict** (not present-with-``None``), so "never wrote
    it" stays distinguishable from "wrote ``None``".

    The framework never chains one Batch's ``exported`` into the next
    Batch's ``params`` — that line is business code (EVO-014 §12).

    Two questions, two fields: ``reason`` is *why this batch ended*,
    ``teardown_outcome`` is *whether the cleanup worked*. A failed
    teardown never rewrites the reason — check both before deciding
    whether it is safe to submit the next Batch.
    """

    reason: ExitReason
    message: str = ""
    exception: BaseException | None = None
    failed_node: str | None = None
    final_status: Status = Status.IDLE
    teardown_outcome: TeardownOutcome = TeardownOutcome.NONE
    exported: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """True only when the main tree ran to SUCCESS.

        Says nothing about the teardown tree — see ``teardown_outcome``.
        """
        return self.reason is ExitReason.COMPLETED

    @property
    def teardown_ok(self) -> bool:
        """True unless a teardown tree was given and failed to complete.

        The safety question: on ``False``, the cleanup the business asked
        for did not finish — the device may not be where (or as still as)
        it is supposed to be.
        """
        return self.teardown_outcome in (
            TeardownOutcome.NONE,
            TeardownOutcome.SUCCEEDED,
        )


class Batch:
    """One unit of work: a tree, its Blackboard, a lifecycle, an exit result.

    ``READY → RUNNING → EXITED``. A Batch runs *once* — but the clock is
    free to take the next one as soon as this one has EXITED, which is
    the whole point of replacing ``Action``.

    Submit it with ``BTClock.submit(batch)``; stop it early with
    ``BTClock.kill(handle)``. There is no blocking ``wait()``: the
    business owns the tick loop, so "waiting for the result" is polling
    ``handle.state`` / ``handle.result`` in that loop.

    Teardown (EVO-014 §7): on **every** exit path — SUCCESS, FAILURE,
    kill — the main tree stops and then the teardown tree, if any, is
    ticked to completion; only then does the Batch reach ``EXITED``. The
    state stays ``RUNNING`` throughout. No new framework mechanism is
    involved: "wait for the arm to actually stop" is a ``WaitFor`` in the
    teardown tree, and "give up after 5 s" is ``.timeout(5.0)`` on it.
    Give no teardown tree and the phase costs zero ticks.

    .. warning::

       **A teardown tree only runs if you keep ticking.** ``kill()``
       *begins* the exit; it does not complete it. The framework has no
       thread of its own — the teardown tree is ticked by the same loop
       that ticked the main tree, so::

           clock.kill(handle)
           while handle.result is None:      # ← do not skip this
               clock.tick_once()
               time.sleep(period)

       Break out of the loop right after ``kill()`` and the teardown tree
       is silently never ticked. This bites hardest on exactly the path
       it was written for: an abnormal stop, where business code is most
       tempted to just leave the loop. ``result is not None`` is the only
       signal that the exit finished; then check
       ``result.teardown_outcome``.
    """

    SLOW_TICK_DEFAULT_BUDGET_S = 0.04  # 40 ms — generous default at 25 Hz

    def __init__(
        self,
        build_tree: TreeFactory,
        *,
        params: dict[str, Any] | None = None,
        export: Iterable[str] = (),
        build_teardown: TreeFactory | None = None,
        batch_no: str | None = None,
        name: str = "",
        tracer: BatchTracer | None = None,
        slow_tick_budget_s: float = SLOW_TICK_DEFAULT_BUDGET_S,
    ):
        """
        Args:
            build_tree: builds the main tree. Called once, here.
            params: frozen into the Blackboard via ``set_initial`` at
                construction and never touched again by the framework,
                which does **not** interpret what is in there.
            export: blackboard keys to lift into ``BatchResult.exported``
                when the Batch exits. Anything not declared is dropped.
            build_teardown: builds the teardown tree. Called once, here.
            batch_no: the business/MES batch number. Not an id — may
                repeat, may be absent.
            name: defaults to the main tree's name.
        """
        self.id = next_batch_id()
        self.batch_no = batch_no

        self.tree = build_tree()
        self.teardown_tree = build_teardown() if build_teardown is not None else None
        self.name = name or self.tree.name

        self._tracer: BatchTracer = tracer if tracer is not None else NullTracer()
        self._slow_tick_budget_s = slow_tick_budget_s
        self._export: tuple[str, ...] = tuple(export)

        # The Batch's private address space (EVO-014 §9). The teardown
        # tree shares it — same process, same memory.
        self._params: dict[str, Any] = dict(params or {})
        self._blackboard = Blackboard()
        self.tree.set_blackboard(self._blackboard)
        if self.teardown_tree is not None:
            self.teardown_tree.set_blackboard(self._blackboard)
        for key, value in self._params.items():
            self._blackboard.set_initial(key, value)

        self._state = BatchState.READY
        self._tick_seq = 0
        # Internal exit bookkeeping. ``_exiting`` is what makes kill()
        # idempotent while the teardown tree runs; it is deliberately NOT
        # promoted to a public state (that would be EXITING by the back
        # door — EVO-014 §7).
        self._exiting = False
        self._pending_result: BatchResult | None = None
        self._result: BatchResult | None = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def state(self) -> BatchState:
        return self._state

    @property
    def result(self) -> BatchResult | None:
        """The exit result, or ``None`` until the Batch has EXITED.

        ``result is not None`` ⇔ ``state is BatchState.EXITED``.
        """
        return self._result

    @property
    def info(self) -> BatchInfo:
        return BatchInfo(id=self.id, batch_no=self.batch_no)

    @property
    def params(self) -> dict[str, Any]:
        """Copy of the params frozen at construction."""
        return dict(self._params)

    @property
    def export_keys(self) -> tuple[str, ...]:
        return self._export

    @property
    def blackboard(self) -> Blackboard:
        """The Batch's address space. Exposed for inspection and tests —
        during a run only BT nodes should write it (EVO-014 §9)."""
        return self._blackboard

    # ------------------------------------------------------------------
    # Framework plumbing
    # ------------------------------------------------------------------

    def set_frames(self, frames) -> None:
        """Inject the cell's coordinate-frame graph into both trees.

        Called by BTClock at submit time when the clock was given a
        ``frames=``.
        """
        self.tree.set_frames(frames)
        if self.teardown_tree is not None:
            self.teardown_tree.set_frames(frames)

    def tick(self, snapshot: Snapshot, ctx: TickContext | None = None) -> Status:
        """Run one tick of whichever tree is current. Called by BTClock.

        Returns the status of the tree that was ticked (the main tree, or
        the teardown tree once the exit path has been entered). After
        EXITED, further calls are no-ops.
        """
        if self._state is BatchState.EXITED:
            return self._result.final_status if self._result else Status.IDLE

        if self._state is BatchState.READY:
            self._state = BatchState.RUNNING
            self._tracer.on_batch_begin(self.name)

        if self._exiting:
            tree = self.teardown_tree
            if tree is None:  # pragma: no cover — _enter_exit finalizes first
                self._finalize(TeardownOutcome.NONE)
                return self._result.final_status if self._result else Status.IDLE
            status = self._tick_tree(tree, snapshot, ctx)
            if status is Status.SUCCESS:
                self._finalize(TeardownOutcome.SUCCEEDED)
            elif status is Status.FAILURE:
                logger.warning(
                    "batch '%s' teardown tree returned FAILURE; exiting anyway "
                    "— the cleanup it describes did NOT complete",
                    self.name,
                )
                self._finalize(TeardownOutcome.FAILED)
            return status

        status = self._tick_tree(self.tree, snapshot, ctx)
        if status is Status.SUCCESS:
            self._enter_exit(
                BatchResult(reason=ExitReason.COMPLETED, final_status=status)
            )
        elif status is Status.FAILURE:
            self._enter_exit(self._build_failure_result(status))
        return status

    def kill(self) -> None:
        """Begin the exit: halt the main tree, then run the teardown tree.

        **``kill()`` starts the exit; it does not finish it.** With a
        teardown tree the Batch is still ``RUNNING`` when this returns,
        and ``result`` is still ``None``. The framework owns no thread —
        **the caller must keep ticking until ``result`` is not None**, or
        the teardown tree never runs at all::

            batch.kill()
            while batch.result is None:
                clock.tick_once()

        This fails *silently* if ignored: leaving the tick loop right
        after ``kill()`` looks like it worked. With no teardown tree the
        Batch reaches EXITED right here, costing zero ticks.

        Idempotent — calling it again while the teardown tree is running
        (or after the Batch has EXITED) does nothing, and in particular
        does not skip or restart the teardown.

        Safe to call from threads other than the clock's tick thread:
        ``tree.halt()`` only walks the tree and invokes node-local
        cleanup.
        """
        if self._state is BatchState.EXITED or self._exiting:
            return

        result = BatchResult(
            reason=ExitReason.KILLED,
            message="killed",
            final_status=self.tree.status,
        )
        try:
            self.tree.halt()
        except Exception:
            logger.exception("batch '%s' main tree halt raised", self.name)
        self._enter_exit(result)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _tick_tree(
        self, tree: TreeNode, snapshot: Snapshot, ctx: TickContext | None
    ) -> Status:
        self._tick_seq += 1
        self._tracer.on_tick_start(self._tick_seq)
        # Wall-clock measurement of how long the tick took to execute —
        # not tree logic time (which comes from the TickContext). Reading
        # the real clock is the right thing here.
        t0 = time.monotonic()

        status = tree.tick(snapshot, ctx)

        duration = time.monotonic() - t0
        self._tracer.on_tick_end(self._tick_seq, duration, status)

        if duration > self._slow_tick_budget_s:
            logger.warning(
                "slow tick in batch '%s': %.1fms (budget %.1fms)",
                self.name,
                duration * 1000,
                self._slow_tick_budget_s * 1000,
            )
            self._tracer.on_slow_tick(duration, self._slow_tick_budget_s)

        return status

    def _enter_exit(self, result: BatchResult) -> None:
        """Enter the exit path with a provisional result.

        The state stays RUNNING while the teardown tree runs; with no
        teardown tree the Batch exits right here, costing zero ticks.
        """
        self._pending_result = result
        self._exiting = True
        if self.teardown_tree is None:
            self._finalize(TeardownOutcome.NONE)

    def _finalize(self, teardown_outcome: TeardownOutcome) -> None:
        result = self._pending_result or BatchResult(reason=ExitReason.KILLED)
        result.teardown_outcome = teardown_outcome
        result.exported = self._collect_exports()
        self._result = result
        self._state = BatchState.EXITED
        self._tracer.on_batch_finish(self.name, result)

    def _abort(self, exc: BaseException | None = None, *, message: str = "") -> None:
        """Force the Batch straight to EXITED without running the teardown tree.

        Two callers, both in ``BTClock``:

          - ``tick()`` itself raised. Whatever raised may well be inside
            the teardown path, and retrying it every tick would wedge the
            loop, so the teardown is skipped and ``reason`` becomes
            ``ERRORED`` carrying the exception.
          - ``shutdown()`` found a Batch mid-teardown. There is no
            exception and nothing went wrong with the Batch itself: the
            reason already decided on the exit path is **kept** (a kill in
            flight stays a kill) and only the teardown is marked
            ``ABORTED``.

        Either way both trees are halted best-effort, so device-level
        goals still get their ``on_halted``.

        This method exists because ``result is not None`` ⇔ ``EXITED`` is
        a contract the business polls on: a Batch that could get stuck
        short of EXITED would hang that loop. Only one Batch may run at a
        time, so one left attached would also stop the machine from ever
        accepting another. Every step below is therefore individually
        guarded — the Batch reaches EXITED even if the tracer or the
        blackboard is the thing that is broken.
        """
        if self._state is BatchState.EXITED:
            return

        if exc is not None:
            result = BatchResult(
                reason=ExitReason.ERRORED,
                message=message or f"batch tick raised {type(exc).__name__}: {exc}",
                exception=exc,
                final_status=self.tree.status,
            )
        else:
            result = self._pending_result or BatchResult(
                reason=ExitReason.KILLED,
                message="killed",
                final_status=self.tree.status,
            )
            if message:
                result.message = message
        # A teardown tree that existed but never got to finish is not the
        # same as never having had one.
        teardown_outcome = (
            TeardownOutcome.NONE
            if self.teardown_tree is None
            else TeardownOutcome.ABORTED
        )

        for tree in (self.tree, self.teardown_tree):
            if tree is None:
                continue
            try:
                tree.halt()
            except BaseException:
                logger.exception(
                    "batch '%s' tree halt raised during abort", self.name
                )

        try:
            result.exported = self._collect_exports()
        except BaseException:
            logger.exception("batch '%s' export collection raised during abort",
                             self.name)

        result.teardown_outcome = teardown_outcome
        self._pending_result = result
        self._exiting = True
        self._result = result
        self._state = BatchState.EXITED

        try:
            self._tracer.on_batch_finish(self.name, result)
        except BaseException:
            logger.exception(
                "batch '%s' tracer on_batch_finish raised during abort",
                self.name,
            )

    def _collect_exports(self) -> dict[str, Any]:
        """Lift the declared keys off the blackboard.

        A declared key the tree never wrote is simply **absent** — never
        filled in with ``None``.
        """
        out: dict[str, Any] = {}
        for key in self._export:
            if self._blackboard.has_key(key):
                out[key] = self._blackboard.read(key)
        return out

    def _build_failure_result(self, final_status: Status) -> BatchResult:
        exc, failed = self._collect_failure_info(self.tree)
        if exc is not None:
            self._tracer.on_node_exception(failed or "<unknown>", exc)
        return BatchResult(
            reason=ExitReason.FAILED if exc is None else ExitReason.ERRORED,
            message=(
                "Tree returned FAILURE" if exc is None else f"node '{failed}' raised"
            ),
            exception=exc,
            failed_node=failed,
            final_status=final_status,
        )

    @staticmethod
    def _collect_failure_info(
        node: TreeNode,
    ) -> tuple[BaseException | None, str | None]:
        """Walk the tree and return the first node carrying an exception."""
        if node._exception is not None:
            return node._exception, node.name
        for child in node._children_list():
            if child is node:
                continue
            exc, name = Batch._collect_failure_info(child)
            if exc is not None:
                return exc, name
        return None, None
