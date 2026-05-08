import threading
import time

import pytest

from autoweaver.motion_policy.world_board import Snapshot, WorldBoard


# ---------------------------------------------------------------------------
# State (bulletin board)
# ---------------------------------------------------------------------------

def test_declare_then_post_and_read():
    board = WorldBoard()
    board.declare_state("dobot1.pose", tuple, writer="dobot1")
    board.post_state("dobot1.pose", (1.0, 2.0, 3.0), writer="dobot1")
    assert board.read_state("dobot1.pose") == (1.0, 2.0, 3.0)


def test_post_undeclared_state_raises():
    board = WorldBoard()
    with pytest.raises(KeyError):
        board.post_state("ns.nope", 1, writer="anyone")


def test_post_wrong_writer_raises():
    board = WorldBoard()
    board.declare_state("dobot1.pose", tuple, writer="dobot1")
    with pytest.raises(PermissionError):
        board.post_state("dobot1.pose", (0.0,), writer="impostor")


def test_post_wrong_type_raises():
    board = WorldBoard()
    board.declare_state("dobot1.running", bool, writer="dobot1")
    with pytest.raises(TypeError):
        board.post_state("dobot1.running", "yes", writer="dobot1")


def test_redeclare_with_conflicting_writer_raises():
    board = WorldBoard()
    board.declare_state("dobot1.pose", tuple, writer="dobot1")
    with pytest.raises(ValueError):
        board.declare_state("dobot1.pose", tuple, writer="other")


def test_snapshot_is_immutable_after_subsequent_posts():
    board = WorldBoard()
    board.declare_state("ns.k", int, writer="w")
    board.post_state("ns.k", 1, writer="w")
    snap = board.snapshot()
    board.post_state("ns.k", 2, writer="w")
    assert snap["ns.k"] == 1
    assert board.snapshot()["ns.k"] == 2


def test_snapshot_seq_monotonic():
    board = WorldBoard()
    board.declare_state("ns.k", int, writer="w")
    seqs = []
    for i in range(5):
        board.post_state("ns.k", i, writer="w")
        seqs.append(board.snapshot().seq)
    assert seqs == sorted(seqs) and len(set(seqs)) == 5


def test_history_window_size_default():
    board = WorldBoard()
    board.declare_state("ns.k", int, writer="w")
    for i in range(WorldBoard.DEFAULT_HISTORY_SIZE + 50):
        board.post_state("ns.k", i, writer="w")
    assert len(board.history()) == WorldBoard.DEFAULT_HISTORY_SIZE


def test_history_of_filters_by_changed_key():
    board = WorldBoard()
    board.declare_state("ns.a", int, writer="w")
    board.declare_state("ns.b", int, writer="w")
    board.post_state("ns.a", 1, writer="w")
    board.post_state("ns.b", 10, writer="w")
    board.post_state("ns.a", 2, writer="w")
    snaps = board.history_of("ns.a")
    assert len(snaps) == 2
    assert [s.data["ns.a"] for s in snaps] == [1, 2]


def test_values_of_returns_recent_values():
    board = WorldBoard()
    board.declare_state("ns.a", int, writer="w")
    for i in range(5):
        board.post_state("ns.a", i, writer="w")
    assert board.values_of("ns.a") == [0, 1, 2, 3, 4]
    assert board.values_of("ns.a", n=2) == [3, 4]


def test_changed_between_filters_by_time():
    board = WorldBoard()
    board.declare_state("ns.a", int, writer="w")
    board.post_state("ns.a", 1, writer="w")
    t_mid = time.monotonic()
    time.sleep(0.001)
    board.post_state("ns.a", 2, writer="w")
    later = board.changed_between("ns.a", t_mid, time.monotonic() + 1.0)
    assert [s.data["ns.a"] for s in later] == [2]


def test_concurrent_state_writes_from_threads():
    """Smoke test: 4 threads each posting 100 times produce 400 history entries."""
    board = WorldBoard(history_size=1000)
    # Each thread owns its own namespace ⇒ no cross-namespace conflict.
    for i in range(4):
        board.declare_state(f"w{i}.value", int, writer=f"w{i}")

    def worker(i: int):
        for j in range(100):
            board.post_state(f"w{i}.value", j, writer=f"w{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert board.snapshot().seq == 400
    assert len(board.history()) == 401  # initial + 400 writes


def test_snapshot_get_and_contains():
    board = WorldBoard()
    board.declare_state("ns.k", int, writer="w")
    board.post_state("ns.k", 1, writer="w")
    snap = board.snapshot()
    assert "ns.k" in snap
    assert snap.get("missing", "default") == "default"


# ---------------------------------------------------------------------------
# Namespace enforcement
# ---------------------------------------------------------------------------

def test_declare_top_level_state_raises():
    board = WorldBoard()
    with pytest.raises(ValueError, match="namespace"):
        board.declare_state("toplevel", int, writer="w")


def test_declare_empty_namespace_raises():
    board = WorldBoard()
    with pytest.raises(ValueError):
        board.declare_state(".rest", int, writer="w")


def test_declare_empty_rest_raises():
    board = WorldBoard()
    with pytest.raises(ValueError):
        board.declare_state("ns.", int, writer="w")


def test_namespace_owned_by_first_writer():
    board = WorldBoard()
    board.declare_state("ns.a", int, writer="alice")
    with pytest.raises(ValueError, match="Namespace 'ns'"):
        board.declare_state("ns.b", int, writer="bob")


def test_same_writer_can_declare_multiple_keys_in_namespace():
    board = WorldBoard()
    board.declare_state("perception.detections", list, writer="perception")
    board.declare_state("perception.stable_targets", list, writer="perception")
    board.post_state("perception.detections", [], writer="perception")
    board.post_state("perception.stable_targets", [1, 2], writer="perception")
    assert board.read_state("perception.stable_targets") == [1, 2]


def test_namespace_owner_introspection():
    board = WorldBoard()
    board.declare_state("perception.x", int, writer="perception")
    assert board.namespace_owner("perception") == "perception"
    assert board.namespace_owner("nothing") is None


def test_declared_states_introspection():
    board = WorldBoard()
    board.declare_state("perception.a", int, writer="perception")
    board.declare_state("motion.pose", tuple, writer="motion")
    assert sorted(board.declared_states()) == ["motion.pose", "perception.a"]


# ---------------------------------------------------------------------------
# Notes (passed slips)
# ---------------------------------------------------------------------------

def test_pass_note_does_not_appear_in_snapshot():
    """Notes never enter the state snapshot — they sit in a pending queue."""
    board = WorldBoard()
    received: list = []
    board.accept_notes("perception", "go", dict, received.append)

    seq_before = board.snapshot().seq
    board.pass_note("perception", "go", {"region": 3}, sender="bt")
    # No state mutation, no new snapshot.
    assert board.snapshot().seq == seq_before
    # And the slip is invisible to read_state — it's not state.
    assert board.read_state("perception.note.go") is None


def test_pass_then_deliver_invokes_receiver():
    board = WorldBoard()
    received: list = []
    board.accept_notes("perception", "go", dict, received.append)
    board.pass_note("perception", "go", {"region": 3}, sender="bt")
    assert received == []
    board.deliver_notes()
    assert received == [{"region": 3}]


def test_pass_unknown_note_raises():
    board = WorldBoard()
    with pytest.raises(KeyError):
        board.pass_note("perception", "missing", {}, sender="bt")


def test_pass_wrong_payload_type_raises():
    board = WorldBoard()
    board.accept_notes("perception", "go", dict, lambda p: None)
    with pytest.raises(TypeError):
        board.pass_note("perception", "go", "not a dict", sender="bt")


def test_multiple_notes_to_same_slot_all_delivered_in_order():
    """Same (namespace, name) passed twice in one cycle ⇒ receiver sees both."""
    board = WorldBoard()
    received: list = []
    board.accept_notes("perception", "go", dict, received.append)
    board.pass_note("perception", "go", {"n": 1}, sender="bt")
    board.pass_note("perception", "go", {"n": 2}, sender="bt")
    board.deliver_notes()
    assert received == [{"n": 1}, {"n": 2}]


def test_deliver_clears_queue():
    """After deliver, the queue is empty — re-deliver is a noop."""
    board = WorldBoard()
    received: list = []
    board.accept_notes("perception", "go", dict, received.append)
    board.pass_note("perception", "go", {"n": 1}, sender="bt")
    board.deliver_notes()
    board.deliver_notes()  # nothing pending
    assert received == [{"n": 1}]


def test_deliver_with_no_pending_is_noop():
    board = WorldBoard()
    board.accept_notes("perception", "go", dict, lambda p: None)
    seq_before = board.snapshot().seq
    board.deliver_notes()
    assert board.snapshot().seq == seq_before


def test_deliver_handles_multiple_slots_independently():
    board = WorldBoard()
    received_a: list = []
    received_b: list = []
    board.accept_notes("perception", "a", dict, received_a.append)
    board.accept_notes("perception", "b", dict, received_b.append)
    board.pass_note("perception", "a", {"x": 1}, sender="bt")
    # No "b" passed.
    board.deliver_notes()
    assert received_a == [{"x": 1}]
    assert received_b == []


def test_one_receiver_failing_does_not_starve_others():
    """If one receiver raises, the rest still run; exception is re-raised."""
    board = WorldBoard()
    received_b: list = []
    board.accept_notes(
        "perception", "a", dict,
        lambda p: (_ for _ in ()).throw(RuntimeError("a exploded")),
    )
    board.accept_notes("perception", "b", dict, received_b.append)
    board.pass_note("perception", "a", {"x": 1}, sender="bt")
    board.pass_note("perception", "b", {"y": 2}, sender="bt")
    with pytest.raises(RuntimeError, match="a exploded"):
        board.deliver_notes()
    # b ran despite a's failure
    assert received_b == [{"y": 2}]


def test_multiple_receivers_failing_grouped_into_exception_group():
    board = WorldBoard()
    board.accept_notes(
        "perception", "a", dict,
        lambda p: (_ for _ in ()).throw(RuntimeError("a")),
    )
    board.accept_notes(
        "perception", "b", dict,
        lambda p: (_ for _ in ()).throw(ValueError("b")),
    )
    board.pass_note("perception", "a", {}, sender="bt")
    board.pass_note("perception", "b", {}, sender="bt")
    with pytest.raises(ExceptionGroup) as info:
        board.deliver_notes()
    assert len(info.value.exceptions) == 2


def test_accept_notes_double_registration_raises():
    board = WorldBoard()
    board.accept_notes("perception", "go", dict, lambda p: None)
    with pytest.raises(ValueError):
        board.accept_notes("perception", "go", dict, lambda p: None)


def test_note_name_with_dots_raises():
    board = WorldBoard()
    with pytest.raises(ValueError, match="dots"):
        board.accept_notes("perception", "go.further", dict, lambda p: None)


def test_accepted_notes_introspection():
    board = WorldBoard()
    board.accept_notes("perception", "go", dict, lambda p: None)
    board.accept_notes("motion", "halt", dict, lambda p: None)
    pairs = sorted(board.accepted_notes())
    assert pairs == [("motion", "halt"), ("perception", "go")]


def test_state_post_does_not_collide_with_note_namespace():
    """A namespace can have both state fields and note slots, owned by the
    same Subsystem. State 'perception.foo' and note ('perception', 'foo')
    coexist — one is a snapshot key, the other is a queue entry."""
    board = WorldBoard()
    board.declare_state("perception.foo", int, writer="perception")
    received: list = []
    board.accept_notes("perception", "foo", dict, received.append)

    board.post_state("perception.foo", 42, writer="perception")
    board.pass_note("perception", "foo", {"x": 1}, sender="bt")
    board.deliver_notes()

    assert board.read_state("perception.foo") == 42
    assert received == [{"x": 1}]
