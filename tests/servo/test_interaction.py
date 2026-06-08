"""Tests for interaction matrix providers."""

from __future__ import annotations

import numpy as np
import pytest

from autoweaver.servo.interaction import (
    ConstantInteractionMatrix,
    InteractionMatrix,
)


def test_constant_returns_same_matrix_regardless_of_features():
    m = np.array([[1.0, 2.0], [3.0, 4.0]])
    provider = ConstantInteractionMatrix(m)
    a = provider.matrix(np.array([10.0, 20.0]))
    b = provider.matrix(np.array([-5.0, 0.0]))
    np.testing.assert_array_equal(a, b)
    np.testing.assert_array_equal(a, m)


def test_constant_satisfies_protocol():
    provider = ConstantInteractionMatrix(np.eye(2))
    assert isinstance(provider, InteractionMatrix)


def test_constant_exposes_shape():
    provider = ConstantInteractionMatrix(np.zeros((2, 3)))
    assert provider.shape == (2, 3)


def test_constant_is_frozen_against_mutation():
    """The frozen matrix cannot be mutated through the returned array —
    guards against a leaf accidentally corrupting the shared provider."""
    provider = ConstantInteractionMatrix(np.eye(2))
    out = provider.matrix(np.zeros(2))
    with pytest.raises(ValueError):
        out[0, 0] = 99.0


def test_constant_copies_input_so_caller_mutation_is_isolated():
    src = np.eye(2)
    provider = ConstantInteractionMatrix(src)
    src[0, 0] = 99.0  # mutate the original after construction
    np.testing.assert_array_equal(provider.matrix(np.zeros(2)), np.eye(2))


def test_constant_rejects_non_2d():
    with pytest.raises(ValueError, match="must be 2-D"):
        ConstantInteractionMatrix(np.array([1.0, 2.0]))


def test_constant_rejects_empty():
    with pytest.raises(ValueError, match="non-empty"):
        ConstantInteractionMatrix(np.zeros((0, 2)))
