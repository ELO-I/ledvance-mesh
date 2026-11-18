"""Node configuration operations (Configuration Client, Mesh Profile §4.3)."""

from __future__ import annotations

from .client import MeshClient
from .keys import NetworkKeys
from .messages import (
    CompositionData,
    Opcode,
    check_config_status,
    config_appkey_add,
    config_composition_data_get,
    config_model_app_bind,
    config_model_publication_set,
    config_node_reset,
)

NET_INDEX = 0
APP_INDEX = 0


async def add_app_key(client: MeshClient, node: int, keys: NetworkKeys) -> None:
    """Give the network AppKey to the node."""
    params = await client.request(
        node,
        config_appkey_add(NET_INDEX, APP_INDEX, keys.app_key),
        Opcode.CONFIG_APPKEY_STATUS,
        use_device_key=True,
    )
    check_config_status(params, "AppKey addition")


async def bind_model(client: MeshClient, node: int, element: int, model_id: int) -> None:
    """Bind the AppKey to a model of one of the node's elements."""
    params = await client.request(
        node,
        config_model_app_bind(element, APP_INDEX, model_id),
        Opcode.CONFIG_MODEL_APP_STATUS,
        use_device_key=True,
    )
    check_config_status(params, f"binding of model {model_id:#06x}")


async def set_publication(
    client: MeshClient,
    node: int,
    element: int,
    model_id: int,
    *,
    publish_address: int,
    ttl: int,
    period: float = 0,
) -> None:
    """Ask a model to publish its state to `publish_address`."""
    params = await client.request(
        node,
        config_model_publication_set(
            element,
            model_id,
            publish_address=publish_address,
            app_index=APP_INDEX,
            ttl=ttl,
            period=period,
        ),
        Opcode.CONFIG_MODEL_PUBLICATION_STATUS,
        use_device_key=True,
    )
    check_config_status(params, f"publication of model {model_id:#06x}")


async def get_composition(client: MeshClient, node: int) -> CompositionData:
    """Read page 0 of the node's composition data."""
    params = await client.request(
        node,
        config_composition_data_get(),
        Opcode.CONFIG_COMPOSITION_DATA_STATUS,
        use_device_key=True,
    )
    return CompositionData.parse(params[1:])  # without the page number


async def reset_node(client: MeshClient, node: int) -> None:
    """Ask the node to forget the network: it becomes unprovisioned again."""
    params = await client.request(
        node, config_node_reset(), Opcode.CONFIG_NODE_RESET_STATUS, use_device_key=True
    )
    check_config_status(params, "node reset")
