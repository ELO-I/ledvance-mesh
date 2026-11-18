"""IV Update on the integration side: following the network, or driving the update."""

from custom_components.ledvance_mesh.iv_update import IvUpdateManager
from custom_components.ledvance_mesh.mesh import (
    IvState,
    MeshNetwork,
    NetworkKeys,
    SecureNetworkBeacon,
)
from custom_components.ledvance_mesh.mesh.iv import MIN_PHASE_DURATION, SEQUENCE_UPDATE_THRESHOLD
from custom_components.ledvance_mesh.sequence import PersistentSequence

KEYS = NetworkKeys(bytes(range(16)), bytes(range(16, 32)))


class FakeStore:
    """Same contract as MeshStateStore for the IV index, without a disk."""

    def __init__(self, sequence: int) -> None:
        self.sequence = PersistentSequence(sequence, lambda _bound: None)
        self.iv = IvState(0)
        self.iv_changed_at = 0.0
        self.now = 0.0

    async def async_set_iv(self, iv: IvState) -> None:
        if iv.tx_index != self.iv.tx_index:
            self.sequence.reset()
        self.iv, self.iv_changed_at = iv, self.now


def _manager(sequence: int = 100) -> tuple[IvUpdateManager, MeshNetwork, FakeStore]:
    store = FakeStore(sequence)
    network = MeshNetwork(KEYS, 0x0001, store.sequence)
    return IvUpdateManager(network, store, clock=lambda: store.now), network, store  # type: ignore[arg-type]


def _beacon(index: int, update: bool) -> SecureNetworkBeacon:
    return SecureNetworkBeacon(iv_index=index, iv_update=update, key_refresh=False)


async def test_follows_an_update_started_by_the_network() -> None:
    manager, network, store = _manager()
    await manager.async_on_beacon(_beacon(1, True))
    assert network.iv == IvState(1, True) and store.sequence.value == 100  # transmission unchanged
    assert not manager.must_announce
    await manager.async_on_beacon(_beacon(1, False))
    assert network.iv == IvState(1) and store.sequence.value == 0  # new index: sequence back to 0


async def test_drives_an_update_when_sequence_numbers_run_low() -> None:
    manager, network, store = _manager(SEQUENCE_UPDATE_THRESHOLD)
    await manager.async_on_beacon(_beacon(0, False))
    await manager.async_advance()
    assert network.iv == IvState(0)  # less than 96 h since the last change: wait

    store.now = MIN_PHASE_DURATION
    await manager.async_advance()
    assert network.iv == IvState(1, True) and manager.must_announce
    assert store.sequence.value == SEQUENCE_UPDATE_THRESHOLD  # old index still in use

    # 96 h later the network still has not followed: do not switch to the new index.
    store.now = 2 * MIN_PHASE_DURATION
    await manager.async_advance()
    assert network.iv == IvState(1, True)

    await manager.async_on_beacon(_beacon(1, True))  # the network followed
    await manager.async_advance()
    assert network.iv == IvState(1) and store.sequence.value == 0
    assert manager.must_announce  # our turn to announce the end of the update
