import threading
import time

import pytest

from autoweaver.motion_policy.world_board import Snapshot, WorldBoard


def test_register_then_write_and_read():
    board = WorldBoard()
    board.register("dobot1.pose", tuple, writer="dobot1")
    board.write("dobot1.pose", (1.0, 2.0, 3.0), writer="dobot1")
    assert board.read("dobot1.pose") == (1.0, 2.0, 3.0)


def test_write_unregistered_key_raises():
    board = WorldBoard()
    with pytest.raises(KeyError):
        board.write("nope", 1, writer="anyone")


def test_write_wrong_writer_raises():
    board = WorldBoard()
    board.register("dobot1.pose", tuple, writer="dobot1")
    with pytest.raises(PermissionError):
        board.write("dobot1.pose", (0.0,), writer="impostor")


def test_write_wrong_type_raises():
    board = WorldBoard()
    board.register("dobot1.running", bool, writer="dobot1")
    with pytest.raises(TypeError):
        board.write("dobot1.running", "yes", writer="dobot1")


def test_register_conflicting_writer_raises():
    board = WorldBoard()
    board.register("dobot1.pose", tuple, writer="dobot1")
    with pytest.raises(ValueError):
        board.register("dobot1.pose", tuple, writer="other")


def test_snapshot_is_immutable_after_subsequent_writes():
    board = WorldBoard()
    board.register("k", int, writer="w")
    board.write("k", 1, writer="w")
    snap = board.snapshot()
    board.write("k", 2, writer="w")
    assert snap["k"] == 1
    assert board.snapshot()["k"] == 2


def test_snapshot_seq_monotonic():
    board = WorldBoard()
    board.register("k", int, writer="w")
    seqs = []
    for i in range(5):
        board.write("k", i, writer="w")
        seqs.append(board.snapshot().seq)
    assert seqs == sorted(seqs) and len(set(seqs)) == 5


def test_history_window_size_default():
    board = WorldBoard()
    board.register("k", int, writer="w")
    for i in range(WorldBoard.DEFAULT_HISTORY_SIZE + 50):
        board.write("k", i, writer="w")
    assert len(board.history()) == WorldBoard.DEFAULT_HISTORY_SIZE


def test_history_of_filters_by_changed_key():
    board = WorldBoard()
    board.register("a", int, writer="w")
    board.register("b", int, writer="w")
    board.write("a", 1, writer="w")
    board.write("b", 10, writer="w")
    board.write("a", 2, writer="w")
    snaps = board.history_of("a")
    assert len(snaps) == 2
    assert [s.data["a"] for s in snaps] == [1, 2]


def test_values_of_returns_recent_values():
    board = WorldBoard()
    board.register("a", int, writer="w")
    for i in range(5):
        board.write("a", i, writer="w")
    assert board.values_of("a") == [0, 1, 2, 3, 4]
    assert board.values_of("a", n=2) == [3, 4]


def test_changed_between_filters_by_time():
    board = WorldBoard()
    board.register("a", int, writer="w")
    board.write("a", 1, writer="w")
    t_mid = time.monotonic()
    time.sleep(0.001)
    board.write("a", 2, writer="w")
    later = board.changed_between("a", t_mid, time.monotonic() + 1.0)
    assert [s.data["a"] for s in later] == [2]


def test_concurrent_writes_from_threads():
    """Smoke test: 4 threads each writing 100 times produce 400 history entries."""
    board = WorldBoard(history_size=1000)
    for i in range(4):
        board.register(f"w{i}", int, writer=f"w{i}")

    def worker(i: int):
        for j in range(100):
            board.write(f"w{i}", j, writer=f"w{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert board.snapshot().seq == 400
    assert len(board.history()) == 401  # initial + 400 writes


def test_snapshot_get_and_contains():
    board = WorldBoard()
    board.register("k", int, writer="w")
    board.write("k", 1, writer="w")
    snap = board.snapshot()
    assert "k" in snap
    assert snap.get("missing", "default") == "default"
