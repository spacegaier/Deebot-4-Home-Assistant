"""Controller module."""
import logging
import random
import string
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from deebot_client.api_client import ApiClient
from deebot_client.authentication import Authenticator
from deebot_client.device import Device
from deebot_client.exceptions import InvalidAuthenticationError
from deebot_client.models import ApiDeviceInfo, Configuration
from deebot_client.mqtt_client import MqttClient, MqttConfiguration
from deebot_client.util import md5
from homeassistant.const import (
    CONF_DEVICES,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.deebot.entity import DeebotEntity, DeebotEntityDescription

from .const import CONF_CLIENT_DEVICE_ID, CONF_CONTINENT, CONF_COUNTRY

_LOGGER = logging.getLogger(__name__)


class DeebotController:
    """Deebot Controller."""

    def __init__(self, hass: HomeAssistant, config: Mapping[str, Any]):
        self._hass_config: Mapping[str, Any] = config
        self._hass: HomeAssistant = hass
        self._devices: list[Device] = []
        verify_ssl = config.get(CONF_VERIFY_SSL, True)
        device_id = config.get(CONF_CLIENT_DEVICE_ID)

        if not device_id:
            # Generate a random device ID on each bootup
            device_id = "".join(
                random.choice(string.ascii_uppercase + string.digits) for _ in range(12)
            )

        deebot_config = Configuration(
            aiohttp_client.async_get_clientsession(self._hass, verify_ssl=verify_ssl),
            device_id=device_id,
            country=config.get(CONF_COUNTRY, "it").lower(),
            continent=config.get(CONF_CONTINENT, "eu").lower(),
            verify_ssl=config.get(CONF_VERIFY_SSL, True),
        )

        self._authenticator = Authenticator(
            deebot_config,
            config.get(CONF_USERNAME, ""),
            md5(config.get(CONF_PASSWORD, "")),
        )
        self._api_client = ApiClient(self._authenticator)

        mqtt_config = MqttConfiguration(config=deebot_config)
        self._mqtt: MqttClient = MqttClient(mqtt_config, self._authenticator)

    async def initialize(self) -> None:
        """Init controller."""
        try:
            await self.teardown()

            devices = await self._api_client.get_devices()

            await self._mqtt.connect()

            for device in devices:
                if device.api_device_info["name"] in self._hass_config.get(
                    CONF_DEVICES, []
                ):
                    bot = Device(device, self._authenticator)
                    _LOGGER.debug(
                        "New vacbot found: %s", device.api_device_info["name"]
                    )
                    await bot.initialize(self._mqtt)
                    self._devices.append(bot)

            _LOGGER.debug("Controller initialize complete")
        except InvalidAuthenticationError as ex:
            raise ConfigEntryAuthFailed from ex
        except Exception as ex:
            msg = "Error during setup"
            _LOGGER.error(msg, exc_info=True)
            raise ConfigEntryNotReady(msg) from ex

    def register_platform_add_entities(
        self,
        entity_class: type[DeebotEntity],
        descriptions: tuple[DeebotEntityDescription, ...],
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Create entities from descriptions and add them."""
        new_entites: list[DeebotEntity] = []

        for device in self._devices:
            for description in descriptions:
                if capability := description.capability_fn(device.capabilities):
                    new_entites.append(entity_class(device, capability, description))

        if new_entites:
            async_add_entities(new_entites)

    def register_platform_add_entities_generator(
        self,
        async_add_entities: AddEntitiesCallback,
        func: Callable[[Device], Sequence[DeebotEntity[Any, EntityDescription]]],
    ) -> None:
        """Add entities generated through the provided function."""
        new_entites: list[DeebotEntity[Any, EntityDescription]] = []

        for device in self._devices:
            new_entites.extend(func(device))

        if new_entites:
            async_add_entities(new_entites)

    def get_device_info(self, device: DeviceEntry) -> ApiDeviceInfo | dict[str, str]:
        """Get the device info for the given entry."""
        for bot in self._devices:
            for identifier in device.identifiers:
                if bot.device_info.did == identifier[1]:
                    return bot.device_info.api_device_info

        _LOGGER.error("Could not find the device with entry: %s", device.json_repr)
        return {"error": "Could not find the device"}

    async def teardown(self) -> None:
        """Disconnect controller."""
        for bot in self._devices:
            await bot.teardown()
        await self._mqtt.disconnect()
        await self._authenticator.teardown()
