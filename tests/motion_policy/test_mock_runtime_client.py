from __future__ import annotations

import pytest

from autoweaver.motion_policy.mock_runtime_client import MockRuntimeClient
from autoweaver.motion_policy.runtime_client import RuntimeFieldError


# ---------------------------------------------------------------------------
# Round-trip writes and reads
# ---------------------------------------------------------------------------

def test_f32_round_trip():
    client = MockRuntimeClient()
    client.write_field_f32("ls6_1", "target_x", 100.5)
    assert client.read_field_f32("ls6_1", "target_x") == 100.5


def test_bool_round_trip():
    client = MockRuntimeClient()
    client.write_field_bool("ls6_1", "trigger", True)
    assert client.read_field_bool("ls6_1", "trigger") is True


def test_i32_round_trip():
    client = MockRuntimeClient()
    client.write_field_i32("ls6_1", "routine", 3)
    assert client.read_field_i32("ls6_1", "routine") == 3


def test_bytes_round_trip():
    client = MockRuntimeClient()
    client.write_field_bytes("ls6_1", "comment", b"hello")
    assert client.read_field_bytes("ls6_1", "comment") == b"hello"


def test_all_numeric_variants_round_trip():
    client = MockRuntimeClient()
    cases = [
        ("write_field_u32", "read_field_u32", 42),
        ("write_field_i64", "read_field_i64", -10**12),
        ("write_field_u64", "read_field_u64", 10**18),
        ("write_field_f64", "read_field_f64", 3.141592653589793),
    ]
    for write_name, read_name, value in cases:
        getattr(client, write_name)("dev", "f", value)
        assert getattr(client, read_name)("dev", "f") == value


# ---------------------------------------------------------------------------
# Multiple devices share one client
# ---------------------------------------------------------------------------

def test_devices_have_independent_fields():
    client = MockRuntimeClient()
    client.write_field_f32("ls6_1", "target_x", 100.0)
    client.write_field_f32("ls6_2", "target_x", 200.0)
    assert client.read_field_f32("ls6_1", "target_x") == 100.0
    assert client.read_field_f32("ls6_2", "target_x") == 200.0


# ---------------------------------------------------------------------------
# Error model
# ---------------------------------------------------------------------------

def test_reading_unset_field_raises():
    client = MockRuntimeClient()
    with pytest.raises(RuntimeFieldError, match="unknown field"):
        client.read_field_f32("ls6_1", "nope")


def test_reading_with_wrong_type_raises():
    client = MockRuntimeClient()
    client.write_field_f32("ls6_1", "target_x", 100.0)
    with pytest.raises(RuntimeFieldError, match="type mismatch"):
        client.read_field_bool("ls6_1", "target_x")


def test_runtime_field_error_carries_device_and_field():
    client = MockRuntimeClient()
    try:
        client.read_field_f32("ls6_1", "missing")
    except RuntimeFieldError as e:
        assert e.device == "ls6_1"
        assert e.field == "missing"
        assert "unknown" in e.reason
    else:
        pytest.fail("expected RuntimeFieldError")


# ---------------------------------------------------------------------------
# Call recording
# ---------------------------------------------------------------------------

def test_calls_record_writes_and_reads_in_order():
    client = MockRuntimeClient()
    client.write_field_f32("ls6_1", "target_x", 1.0)
    client.write_field_i32("ls6_1", "routine", 3)
    client.read_field_f32("ls6_1", "target_x")
    assert client.calls == [
        ("write", "ls6_1", "target_x", 1.0),
        ("write", "ls6_1", "routine", 3),
        ("read", "ls6_1", "target_x", 1.0),
    ]


# ---------------------------------------------------------------------------
# preload helper bypasses call recording
# ---------------------------------------------------------------------------

def test_preload_seeds_without_recording():
    client = MockRuntimeClient()
    client.preload("ls6_1", "done", True, "v_bool")
    assert client.read_field_bool("ls6_1", "done") is True
    # The read is recorded, but the preload itself isn't.
    assert client.calls == [("read", "ls6_1", "done", True)]


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

def test_context_manager_returns_self():
    with MockRuntimeClient() as client:
        assert isinstance(client, MockRuntimeClient)
        client.write_field_bool("dev", "f", True)
