# Ledvance Mesh for Home Assistant

Local control of **Ledvance / Sylvania SMART+ Bluetooth Mesh** bulbs from Home Assistant, over
Bluetooth: no cloud, no SMART+ app, no extra hub.

Each bulb becomes a `light` entity with on/off, brightness, colour (hue/saturation), tunable white
and transitions. Bulbs publish their state themselves, so changes show up in Home Assistant right
away, and a bulb switched off at the wall becomes `unavailable`.

## Supported devices

| Device | Status |
|---|---|
| Sylvania SMART+ Bluetooth Full Color A19, model `60A19CBLE` (sold at Dollarama, Canada) | Tested (5 bulbs) |
| Other Ledvance / Sylvania SMART+ **Bluetooth Mesh** colour bulbs | Untested, may work |
| SMART+ white-only or tunable-white-only bulbs | Not supported yet |
| SMART+ WiFi or Zigbee products | Not supported (different protocols) |

When a lamp is added, the integration reads the mesh models it implements. A device that lacks
the models it needs is refused with an explicit message and reset, instead of being half
configured. If you try another bulb, please open an issue with the result, whether it works or
not, and the diagnostics (they contain no keys).

## Requirements

- Home Assistant 2026.10.4 or later.
- A Bluetooth adapter usable by Home Assistant. The built-in adapter of a Raspberry Pi 4 is
  what this was tested with; an ESPHome Bluetooth proxy with active connections should work too,
  but is untested. Only one bulb needs to be in range: bulbs relay messages to each other.

## Installation

**HACS**: *HACS → ⋮ → Custom repositories*, add `https://github.com/ELO-I/ledvance-mesh` as an
*Integration*, install **Ledvance Mesh**, then restart Home Assistant.

**Manual**: copy `custom_components/ledvance_mesh` into the `custom_components` folder of your
configuration, then restart Home Assistant.

## Usage

1. *Settings → Devices & services → Add integration → Ledvance Mesh*. This creates a private mesh
   network with random keys, stored in the config entry.
2. Reset the bulb: switch it on and off 5 times, until it blinks.
3. On the integration page, select **Add lamp**, pick the bulb and name it. Pairing and
   configuration take about 20 seconds.

A bulb paired with Home Assistant no longer answers the SMART+ app: a mesh network has a single
owner.

- **Removing a lamp** from Home Assistant resets it, so it can be paired again. If it is
  unreachable at that moment, reset it by hand.
- **A lamp reset by mistake** (5 quick on/off cycles): open its menu on the integration page and
  select **Re-pair lamp**. It keeps its name, entity and automations.

### Restart event

When a bulb restarts (power cut then restored, even briefly), the integration fires a
`ledvance_mesh_lamp_restarted` event with `entity_id` and `subentry_id`. It lets a wall switch be
used as a toggle: a quick off/on at the switch triggers an automation.

```yaml
triggers:
  - trigger: event
    event_type: ledvance_mesh_lamp_restarted
    event_data:
      entity_id: light.bedside
actions:
  - action: light.toggle
    target:
      entity_id: light.bedroom
```

## How it works

Home Assistant joins the mesh network as a node and keeps a permanent Bluetooth connection with one
bulb, used as a proxy. Each bulb is configured to publish its state to a group address on every
change, and every 60 seconds as a heartbeat. There is no polling.

Presence of a bulb is tracked from three clues: the proxy connection, its Bluetooth advertisements
(about one per second) and its mesh messages. When its advertisements stop for 15 seconds, the bulb
is asked; without an answer it becomes `unavailable`. A bulb out of the adapter's range is only
tracked through its heartbeat (`unavailable` after about 3.5 minutes).

The colour/white mode is not reported by the bulb: it is inferred from what changes (hue or
temperature).

## Known limitations

- **IV Update**: after many years of normal use, the message counter of the network runs out and
  the integration renews it with the standard IV Update procedure. That procedure has never run in
  full on real bulbs (it takes 96 hours per phase).
- **Brightness and colour units** are those of the tested bulbs (lightness 1-50, hue in degrees,
  saturation in percent), not the standard 16-bit scales. Bulbs using standard scales would need
  another profile (`profile.py`).
- **White range** is 2700-6500 K: the tested bulbs report 1000-12000 K, but their output no longer
  changes below 2700 K.
- **Topology**: a single proxy bulb is used at a time; a bulb out of range of every other bulb and
  of the adapter is unreachable.
- **Power-on behaviour** is the bulb's factory setting; it is not configurable here.
- One mesh network per Home Assistant instance.

## Troubleshooting

- **No lamp listed in "Add lamp"**: the bulb must be reset (it blinks) and in range of the
  adapter. Tick *Refresh the list* and submit to update the list.
- **Debug logs**:

  ```yaml
  logger:
    logs:
      custom_components.ledvance_mesh: debug
  ```

- **Diagnostics** (*integration page → ⋮ → Download diagnostics*) contain the network state
  without any key.

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy custom_components
```

## Disclaimer

This project is not affiliated with or endorsed by Ledvance or Sylvania. It implements the public
Bluetooth Mesh specification. Use it at your own risk.

## License

[MIT](LICENSE)
