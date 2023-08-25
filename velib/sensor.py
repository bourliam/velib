"""Sensor for the Velib data."""
from __future__ import annotations

import asyncio
import voluptuous as vol
from datetime import timedelta
import logging
import aiohttp


from homeassistant.core import HomeAssistant
from homeassistant.exceptions import PlatformNotReady
from homeassistant.components.sensor import (
    PLATFORM_SCHEMA,
    SensorEntity,
    ENTITY_ID_FORMAT,
)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from homeassistant.helpers.typing import (
    ConfigType,
    DiscoveryInfoType,
)

_LOGGER = logging.getLogger(__name__)

CONF_STATIONS_CODE = "stations"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_STATIONS_CODE): vol.All(
            cv.ensure_list, vol.Length(min=1), [cv.string]
        )
    }
)

DEFAULT_ENDPOINT = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/velib-disponibilite-en-temps-reel/records?where=stationcode%20IN%20({station_codes})"


PLATFORM = "velib"
MONITORED_NETWORKS = "monitored-networks"
VELIB_NETWORK = "velib"
SCAN_INTERVAL = timedelta(minutes=5)  # Timely, and doesn't suffocate the API
REQUEST_TIMEOUT = 5  # In seconds; argument to asyncio.timeout
VELIB_ATTRIBUTION = "Information provided by Velib"


ATTR_STATION_CODE = "stationcode"
ATTR_NAME = "name"
ATTR_DATETIME = "duedate"
ATTR_DOCKS_AVAILABLE = "numdocksavailable"
ATTR_BIKES_AVAILABLE = "numbikesavailable"
ATTR_MECHANICAL = "mechanical"
ATTR_ELECTRIC = "ebike"
ATTR_CAPACITY = "capacity"
ATTR_INSTALLED = "is_installed"
ATTR_RENTING = "is_renting"
ATTR_RETURNING = "is_returning"
ATTR_CITY = "nom_arrondissement_communes"


STATION_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_STATION_CODE): cv.string,
        vol.Required(ATTR_NAME): cv.string,
        vol.Required(ATTR_INSTALLED): cv.string,
        vol.Required(ATTR_CAPACITY): cv.positive_int,
        vol.Required(ATTR_DOCKS_AVAILABLE): cv.positive_int,
        vol.Required(ATTR_BIKES_AVAILABLE): cv.positive_int,
        vol.Required(ATTR_MECHANICAL): cv.positive_int,
        vol.Required(ATTR_ELECTRIC): cv.positive_int,
        vol.Required(ATTR_RENTING): cv.string,
        vol.Required(ATTR_RETURNING): cv.string,
        vol.Required(ATTR_DATETIME): cv.string,
        vol.Required(ATTR_CITY): cv.string,
    },
    extra=vol.REMOVE_EXTRA,
)

STATIONS_RESPONSE_SCHEMA = vol.Schema(
    {
        vol.Required("total_count"): vol.All(cv.positive_int),
        vol.Required("results"): [STATION_SCHEMA],
    }
)


class VelibRequestError(Exception):
    """Error to indicate a Velib API request has failed."""


async def async_velib_request(hass: HomeAssistant, schema, station_codes):
    """Perform a request to CityBikes API endpoint, and parse the response."""
    try:
        session = async_get_clientsession(hass)
        async with asyncio.timeout(REQUEST_TIMEOUT):
            req = await session.get(
                DEFAULT_ENDPOINT.format(station_codes=station_codes)
            )

        json_response = await req.json()
        _LOGGER.info(json_response)
        return schema(json_response)
    except (asyncio.TimeoutError, aiohttp.ClientError):
        _LOGGER.error("Could not connect to Velib API endpoint")
    except ValueError:
        _LOGGER.error("Received non-JSON data from Velib API endpoint")
    except vol.Invalid as err:
        _LOGGER.error("Received unexpected JSON from Velib API endpoint: %s", err)
    raise VelibRequestError


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the velib platform."""

    if PLATFORM not in hass.data:
        hass.data[PLATFORM] = {MONITORED_NETWORKS: {}}

    station_codes = set(config.get(CONF_STATIONS_CODE, []))
    network_id = VELIB_NETWORK

    if network_id not in hass.data[PLATFORM][MONITORED_NETWORKS]:
        network = VelibNetwork(hass, network_id, station_codes)
        hass.data[PLATFORM][MONITORED_NETWORKS][network_id] = network
        hass.async_create_task(network.async_refresh())
        async_track_time_interval(hass, network.async_refresh, SCAN_INTERVAL)
    else:
        network = hass.data[PLATFORM][MONITORED_NETWORKS][network_id]

    await network.ready.wait()

    devices = []
    for station in network.stations:
        station_id = station[ATTR_STATION_CODE]
        _LOGGER.info("FOUND" + station_id)

        entity_id = async_generate_entity_id(ENTITY_ID_FORMAT, station_id, hass=hass)
        _LOGGER.info("Create entity " + entity_id)

        devices.append(VelibStation(network, station_id, entity_id))

    async_add_entities(devices, True)


class VelibNetwork:
    """Wrapper around a Velib network object."""

    def __init__(self, hass, network_id, station_codes) -> None:
        """Initialize the network object."""
        self.hass = hass
        self.network_id = network_id
        self.station_codes = station_codes
        self.stations = []
        self.ready = asyncio.Event()

    async def async_refresh(self, now=None):
        """Refresh the state of the network."""
        try:
            station_codes = '"' + '","'.join(self.station_codes) + '"'
            network = await async_velib_request(
                self.hass, STATIONS_RESPONSE_SCHEMA, station_codes
            )
            self.stations = network["results"]
            self.ready.set()
        except VelibRequestError as err:
            if now is not None:
                self.ready.clear()
            else:
                raise PlatformNotReady from err


class VelibStation(SensorEntity):
    """Velib API Sensor."""

    _attr_attribution = VELIB_ATTRIBUTION
    _attr_native_unit_of_measurement = "bikes"
    _attr_icon = "mdi:bike"

    def __init__(self, network, station_id, entity_id) -> None:
        """Initialize the sensor."""
        self._network = network
        self._station_id = station_id
        self.entity_id = entity_id

    async def async_update(self) -> None:
        """Update station state."""
        for station in self._network.stations:
            if station[ATTR_STATION_CODE] == self._station_id:
                station_data = station
                break
        self._attr_name = station_data.get(ATTR_NAME)
        self._attr_native_value = station_data.get(ATTR_BIKES_AVAILABLE)
        self._attr_extra_state_attributes = {
            ATTR_MECHANICAL: station_data.get(ATTR_MECHANICAL),
            ATTR_ELECTRIC: station_data.get(ATTR_ELECTRIC),
            ATTR_DATETIME: station_data.get(ATTR_DATETIME),
        }
