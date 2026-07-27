"""Tests for the comm primitive engine (EVO-009).

Hardware-free: a ``FakeRegisterIO`` stands in for pymodbus, and a fake clock
drives ``read_until`` deterministically (no wall-clock sleeping).
"""

from __future__ import annotations

import pytest

from autoweaver.comm.modbus_primitive import (
    ActionStepError,
    BlockSpec,
    Clock,
    CommContract,
    CommEngine,
    ReadUntilTimeout,
)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeRegisterIO:
    """In-memory registers + REAL32 blocks. Records every write for assertions
    and lets tests script register values (incl. a value that flips after N
    reads, to exercise read_until's polling)."""

    def __init__(self) -> None:
        self.regs: dict[int, int] = {}
        self.blocks: dict[int, list[float]] = {}
        self.writes: list[tuple] = []
        # register -> list of values to return on successive reads (last value
        # sticks once exhausted). If absent, regs[...] (default 0) is returned.
        self._read_scripts: dict[int, list[int]] = {}

    # RegisterIO interface
    def read_u16(self, register: int) -> int:
        script = self._read_scripts.get(register)
        if script:
            return script.pop(0) if len(script) > 1 else script[0]
        return self.regs.get(register, 0)

    def write_u16(self, register: int, value: int) -> None:
        self.regs[register] = value
        self.writes.append(("u16", register, value))

    def read_real32_block(self, start: int, count: int) -> list[float]:
        return list(self.blocks.get(start, [0.0] * count))

    def write_real32_block(self, start: int, values) -> None:
        self.blocks[start] = list(values)
        self.writes.append(("block", start, list(values)))

    # test helpers
    def script_reads(self, register: int, values: list[int]) -> None:
        self._read_scripts[register] = list(values)


class FakeClock:
    """Deterministic clock. Time only advances when sleep() is called."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps = 0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, dt: float) -> None:
        self.t += dt
        self.sleeps += 1

    def as_clock(self) -> Clock:
        return Clock(monotonic=self.monotonic, sleep=self.sleep)


# --------------------------------------------------------------------------- #
# Fixtures: a contract resembling the pluck PLC rig.
# --------------------------------------------------------------------------- #
@pytest.fixture
def contract() -> CommContract:
    return CommContract(
        registers={
            "plc_send": 41068,
            "pc_send": 41168,
            "pc_func": 41169,
            "wash_done": 41200,
        },
        blocks={
            "cmd_pose": BlockSpec(start=41183, count=6, order=("x", "y", "z", "rz", "ry", "rx")),
            "rt_pose": BlockSpec(start=41115, count=6, order=("x", "y", "z", "rz", "ry", "rx")),
        },
        constants={"SET": 1, "CLEAR": 0, "NONE": 0, "COORD": 1, "GRASP": 10, "WASH": 20},
    )


@pytest.fixture
def io() -> FakeRegisterIO:
    return FakeRegisterIO()


def make_engine(contract, io, clock=None):
    return CommEngine(contract, io, clock=clock, poll_interval_s=1.0)


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #
def test_write_flags_resolves_constants(contract, io):
    eng = make_engine(contract, io)
    eng.run_action([{"write": {"flags": {"pc_send": "SET", "pc_func": "GRASP"}}}])
    assert io.regs[41168] == 1
    assert io.regs[41169] == 10


def test_write_register_with_raw_int(contract, io):
    eng = make_engine(contract, io)
    eng.run_action([{"write": {"register": "pc_send", "value": 1}}])
    assert io.regs[41168] == 1


def test_write_block_reorders_pose_to_wire_order(contract, io):
    eng = make_engine(contract, io)
    pose = {"x": 1.0, "y": 2.0, "z": 3.0, "rx": 4.0, "ry": 5.0, "rz": 6.0}
    eng.run_action(
        [{"write": {"block": "cmd_pose", "values": "$pose"}}],
        params={"pose": pose},
    )
    # cmd_pose order is (x, y, z, rz, ry, rx) -> rz/ry/rx in last three slots.
    assert io.blocks[41183] == [1.0, 2.0, 3.0, 6.0, 5.0, 4.0]


def test_write_block_rejects_incomplete_pose(contract, io):
    eng = make_engine(contract, io)
    with pytest.raises(ActionStepError):
        eng.run_action(
            [{"write": {"block": "cmd_pose", "values": "$pose"}}],
            params={"pose": {"x": 1.0}},
        )


# --------------------------------------------------------------------------- #
# write — path (multi-waypoint) form: values is a list of poses
# --------------------------------------------------------------------------- #
def test_write_block_path_writes_points_consecutively(contract, io):
    eng = make_engine(contract, io)
    p1 = {"x": 1.0, "y": 2.0, "z": 3.0, "rx": 4.0, "ry": 5.0, "rz": 6.0}
    p2 = {"x": 7.0, "y": 8.0, "z": 9.0, "rx": 10.0, "ry": 11.0, "rz": 12.0}
    eng.run_action(
        [{"write": {"block": "cmd_pose", "values": "$path"}}],
        params={"path": [p1, p2]},
    )
    # each point reordered to wire order (x, y, z, rz, ry, rx), laid end to end
    assert io.blocks[41183] == [
        1.0, 2.0, 3.0, 6.0, 5.0, 4.0,
        7.0, 8.0, 9.0, 12.0, 11.0, 10.0,
    ]


def test_write_block_single_pose_unchanged(contract, io):
    # backward compat: a lone Mapping still writes exactly one point.
    eng = make_engine(contract, io)
    pose = {"x": 1.0, "y": 2.0, "z": 3.0, "rx": 4.0, "ry": 5.0, "rz": 6.0}
    eng.run_action(
        [{"write": {"block": "cmd_pose", "values": "$pose"}}],
        params={"pose": pose},
    )
    assert io.blocks[41183] == [1.0, 2.0, 3.0, 6.0, 5.0, 4.0]


def test_write_block_empty_path_raises(contract, io):
    eng = make_engine(contract, io)
    with pytest.raises(ActionStepError):
        eng.run_action(
            [{"write": {"block": "cmd_pose", "values": "$path"}}],
            params={"path": []},
        )


def test_write_block_path_rejects_incomplete_point(contract, io):
    eng = make_engine(contract, io)
    good = {"x": 1.0, "y": 2.0, "z": 3.0, "rx": 4.0, "ry": 5.0, "rz": 6.0}
    with pytest.raises(ActionStepError):
        eng.run_action(
            [{"write": {"block": "cmd_pose", "values": "$path"}}],
            params={"path": [good, {"x": 1.0}]},
        )


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #
def test_read_register_returns_value(contract, io):
    io.regs[41068] = 7
    eng = make_engine(contract, io)
    out = eng.run_action([{"read": {"register": "plc_send"}}])
    assert out["value"] == 7


def test_read_block_maps_back_to_canonical_fields(contract, io):
    # rt_pose wire order (x,y,z,rz,ry,rx); raw block in that order.
    io.blocks[41115] = [10.0, 20.0, 30.0, 60.0, 50.0, 40.0]
    eng = make_engine(contract, io)
    out = eng.run_action([{"read": {"block": "rt_pose", "into": "pose"}}])
    assert out["pose"] == {"x": 10.0, "y": 20.0, "z": 30.0, "rz": 60.0, "ry": 50.0, "rx": 40.0}


def test_read_default_into_key(contract, io):
    io.regs[41068] = 3
    eng = make_engine(contract, io)
    out = eng.run_action([{"read": {"register": "plc_send"}}])
    assert "value" in out


# --------------------------------------------------------------------------- #
# read_until
# --------------------------------------------------------------------------- #
def test_read_until_returns_when_already_satisfied(contract, io):
    io.regs[41068] = 0
    clock = FakeClock()
    eng = make_engine(contract, io, clock.as_clock())
    eng.run_action(
        [{"read_until": {"register": "plc_send", "equals": "CLEAR", "timeout_s": 5}}]
    )
    assert clock.sleeps == 0  # satisfied on first read, never slept


def test_read_until_polls_until_satisfied(contract, io):
    # Register reads 1,1,1,0 -> satisfied on the 4th read.
    io.script_reads(41068, [1, 1, 1, 0])
    clock = FakeClock()
    eng = make_engine(contract, io, clock.as_clock())
    eng.run_action(
        [{"read_until": {"register": "plc_send", "equals": "CLEAR", "timeout_s": 60}}]
    )
    assert clock.sleeps == 3  # slept three times before the 4th read satisfied


def test_read_until_times_out(contract, io):
    io.regs[41068] = 1  # never reaches CLEAR
    clock = FakeClock()
    eng = make_engine(contract, io, clock.as_clock())
    with pytest.raises(ReadUntilTimeout) as exc:
        eng.run_action(
            [{"read_until": {"register": "plc_send", "equals": "CLEAR", "timeout_s": 3}}]
        )
    assert exc.value.register == 41068
    assert exc.value.last_seen == 1
    assert exc.value.timeout_s == 3.0  # declared number is honoured verbatim


# --- timeout_s declaration: number bounds the wait, explicit null does not --- #
def test_read_until_null_timeout_waits_far_past_any_deadline(contract, io):
    # Satisfied only on the 201st read. With poll_interval 1.0s the fake clock
    # reaches t=200s — a numeric timeout of 3s (see the test above) would have
    # raised long before. Declared null => no timeout semantics, keep waiting.
    io.script_reads(41068, [1] * 200 + [0])
    clock = FakeClock()
    eng = make_engine(contract, io, clock.as_clock())
    eng.run_action(
        [{"read_until": {"register": "plc_send", "equals": "CLEAR", "timeout_s": None}}]
    )
    assert clock.sleeps == 200
    assert clock.t == 200.0  # blew past any timeout a caller might have set


def test_read_until_null_timeout_returns_immediately_when_satisfied(contract, io):
    # "Wait forever" must not mean "wait at all" — the predicate still wins.
    io.regs[41068] = 0
    clock = FakeClock()
    eng = make_engine(contract, io, clock.as_clock())
    eng.run_action(
        [{"read_until": {"register": "plc_send", "equals": "CLEAR", "timeout_s": None}}]
    )
    assert clock.sleeps == 0


def test_read_until_missing_timeout_is_a_schema_error_not_forever(contract, io):
    # Explicit null is a declaration; an absent key is an oversight. They must
    # not behave alike — a forgotten field silently waiting forever is the
    # hardest bug to find. Register never satisfies, so a hang would show as one.
    io.regs[41068] = 1
    clock = FakeClock()
    eng = make_engine(contract, io, clock.as_clock())
    with pytest.raises(ActionStepError) as exc:
        eng.run_action([{"read_until": {"register": "plc_send", "equals": "CLEAR"}}])
    assert "timeout_s" in str(exc.value)
    assert clock.sleeps == 0  # rejected up front, never entered the poll loop


# --------------------------------------------------------------------------- #
# action interpreter — composition & atomicity
# --------------------------------------------------------------------------- #
def test_move_pose_action_full_sequence(contract, io):
    # PLC clears its request flag on the 2nd read -> done.
    io.script_reads(41068, [1, 0])
    clock = FakeClock()
    eng = make_engine(contract, io, clock.as_clock())
    pose = {"x": 1.0, "y": 2.0, "z": 3.0, "rx": 4.0, "ry": 5.0, "rz": 6.0}
    steps = [
        {"write": {"block": "cmd_pose", "values": "$pose"}},
        {"write": {"flags": {"pc_send": "SET", "pc_func": "COORD"}}},
        {"read_until": {"register": "plc_send", "equals": "CLEAR", "timeout_s": 120}},
        {"write": {"flags": {"pc_send": "CLEAR", "pc_func": "NONE"}}},
    ]
    eng.run_action(steps, params={"pose": pose})

    # block written in wire order, flags set then cleared, in order.
    assert io.blocks[41183] == [1.0, 2.0, 3.0, 6.0, 5.0, 4.0]
    assert io.regs[41168] == 0  # pc_send ended CLEAR
    assert io.regs[41169] == 0  # pc_func ended NONE
    verbs = [w[0] for w in io.writes]
    assert verbs == ["block", "u16", "u16", "u16", "u16"]


def test_action_aborts_midway_on_timeout_leaving_no_cleanup(contract, io):
    # read_until never satisfied -> action raises; the final clear-flags step
    # never runs. (Atomicity is all-or-nothing from the caller's view; the
    # Worker turns this into an error state — engine does not auto-rollback.)
    io.regs[41068] = 1
    clock = FakeClock()
    eng = make_engine(contract, io, clock.as_clock())
    steps = [
        {"write": {"flags": {"pc_send": "SET", "pc_func": "GRASP"}}},
        {"read_until": {"register": "plc_send", "equals": "CLEAR", "timeout_s": 2}},
        {"write": {"flags": {"pc_send": "CLEAR"}}},  # should NOT run
    ]
    with pytest.raises(ReadUntilTimeout):
        eng.run_action(steps)
    assert io.regs[41168] == 1  # left SET — the clear step never executed


def test_wash_is_move_pose_with_a_different_watch_register(contract, io):
    # "D is B with a different listen position" — same shape, read_until on
    # wash_done instead of plc_send.
    io.script_reads(41200, [0, 0, 1])
    clock = FakeClock()
    eng = make_engine(contract, io, clock.as_clock())
    steps = [
        {"write": {"flags": {"pc_send": "SET", "pc_func": "WASH"}}},
        {"read_until": {"register": "wash_done", "equals": "SET", "timeout_s": 60}},
        {"write": {"flags": {"pc_send": "CLEAR", "pc_func": "NONE"}}},
    ]
    eng.run_action(steps)
    assert io.regs[41169] == 0  # ended NONE


# --------------------------------------------------------------------------- #
# malformed steps — engine guards (loader should catch first, but defense)
# --------------------------------------------------------------------------- #
def test_unknown_verb_raises(contract, io):
    eng = make_engine(contract, io)
    with pytest.raises(ActionStepError):
        eng.run_action([{"teleport": {}}])


def test_multi_verb_step_raises(contract, io):
    eng = make_engine(contract, io)
    with pytest.raises(ActionStepError):
        eng.run_action([{"write": {}, "read": {}}])


def test_unknown_register_name_raises(contract, io):
    eng = make_engine(contract, io)
    with pytest.raises(ActionStepError):
        eng.run_action([{"read": {"register": "nonexistent"}}])


def test_unknown_constant_raises(contract, io):
    eng = make_engine(contract, io)
    with pytest.raises(ActionStepError):
        eng.run_action([{"write": {"flags": {"pc_send": "BOGUS"}}}])


def test_missing_runtime_param_raises(contract, io):
    eng = make_engine(contract, io)
    with pytest.raises(ActionStepError):
        eng.run_action([{"write": {"block": "cmd_pose", "values": "$pose"}}])  # no params
