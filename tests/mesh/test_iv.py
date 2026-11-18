"""IV index: transmit and receive rules, and reaction to beacons."""

from custom_components.ledvance_mesh.mesh import IvState, NetworkKeys
from custom_components.ledvance_mesh.mesh.beacon import (
    SecureNetworkBeacon,
    decode_secure_network_beacon,
    encode_secure_network_beacon,
)

# Mesh Profile 1.0.1, §8.4.3 (Secure Network beacon sample).
SPEC_KEYS = NetworkKeys(bytes.fromhex("7dd7364cd842ad18c17c2b820c84c3d6"), bytes(16))
SPEC_BEACON = bytes.fromhex("01003ecaff672f673370123456788ea261582f364f6f")


def test_beacon_matches_the_specification_sample() -> None:
    assert SPEC_KEYS.beacon_key.hex() == "5423d967da639a99cb02231a83f7d254"
    assert encode_secure_network_beacon(SPEC_KEYS, IvState(0x12345678)) == SPEC_BEACON
    assert decode_secure_network_beacon(SPEC_KEYS, SPEC_BEACON) == SecureNetworkBeacon(
        iv_index=0x12345678, iv_update=False, key_refresh=False
    )


def test_forged_or_foreign_beacon_is_rejected() -> None:
    forged = bytearray(SPEC_BEACON)
    forged[12] ^= 1  # IV index changed without redoing the authentication
    assert decode_secure_network_beacon(SPEC_KEYS, bytes(forged)) is None
    other = NetworkKeys(bytes(range(16)), bytes(16))
    assert decode_secure_network_beacon(other, SPEC_BEACON) is None


def test_update_in_progress_transmits_with_the_old_index_and_accepts_both() -> None:
    normal, updating = IvState(4), IvState(5, True)
    assert (normal.tx_index, updating.tx_index) == (4, 4)
    assert (updating.rx_index(1), updating.rx_index(0)) == (5, 4)
    assert (normal.rx_index(0), normal.rx_index(1)) == (4, 3)
    assert IvState(0).rx_index(1) is None  # no index before 0
    assert normal.begin_update() == updating and updating.complete_update() == IvState(5)


def test_beacons_move_the_state_forward_only() -> None:
    normal = IvState(4)
    assert normal.after_beacon(5, True) == IvState(5, True)  # the network starts an update
    assert IvState(5, True).after_beacon(5, False) == IvState(5)  # ... then completes it
    assert normal.after_beacon(5, False) == IvState(5)  # "in progress" phase missed
    assert normal.after_beacon(9, True) == IvState(9, True)  # long absence: catching up
    assert normal.after_beacon(3, False) == normal  # late beacon
    assert normal.after_beacon(4, True) == normal  # node still "in progress": late as well
    assert normal.after_beacon(4 + 43, False) == normal  # implausible gap: ignored
