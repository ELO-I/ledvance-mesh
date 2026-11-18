"""Persistent sequence counter."""

import pytest

from custom_components.ledvance_mesh.mesh.errors import SequenceExhaustedError
from custom_components.ledvance_mesh.sequence import BLOCK_SIZE, PersistentSequence


def test_numbers_are_strictly_increasing_and_start_at_the_persisted_bound() -> None:
    sequence = PersistentSequence(5000, lambda _bound: None)
    assert [sequence.next() for _ in range(3)] == [5000, 5001, 5002]


def test_reserve_is_extended_before_it_runs_out() -> None:
    persisted: list[int] = []
    sequence = PersistentSequence(0, persisted.append)
    used = [sequence.next() for _ in range(3 * BLOCK_SIZE)]
    assert used == list(range(3 * BLOCK_SIZE))
    assert persisted, "the reserve must be extended"
    # At any time, the persisted bound is above the last number used.
    assert persisted[-1] > used[-1]


def test_restart_never_reuses_a_number_even_after_a_crash() -> None:
    persisted: list[int] = [BLOCK_SIZE]  # initial state persisted by the store
    first = PersistentSequence(0, persisted.append)
    used = [first.next() for _ in range(700)]
    # "Crash": resume from the last persisted bound.
    second = PersistentSequence(persisted[-1], persisted.append)
    assert second.next() > max(used)


def test_exhaustion_is_reported() -> None:
    sequence = PersistentSequence(0xFFFFFF, lambda _bound: None)
    assert sequence.next() == 0xFFFFFF
    with pytest.raises(SequenceExhaustedError):
        sequence.next()


def test_reset_restarts_from_zero_with_a_fresh_reserve() -> None:
    sequence = PersistentSequence(9000, lambda _bound: None)
    sequence.next()
    sequence.reset()
    assert sequence.next() == 0 and sequence.reserved == BLOCK_SIZE
