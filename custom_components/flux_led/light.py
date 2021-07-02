"""Support for FluxLED/MagicHome lights."""

from datetime import timedelta
import logging
import random

from flux_led import WifiLedBulb
import voluptuous as vol

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ATTR_WHITE_VALUE,
    EFFECT_COLORLOOP,
    EFFECT_RANDOM,
    PLATFORM_SCHEMA,
    SUPPORT_BRIGHTNESS,
    SUPPORT_COLOR,
    SUPPORT_COLOR_TEMP,
    SUPPORT_EFFECT,
    SUPPORT_WHITE_VALUE,
    LightEntity,
)
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import (
    ATTR_MODE,
    ATTR_NAME,
    CONF_DEVICES,
    CONF_HOST,
    CONF_NAME,
    CONF_PROTOCOL,
)
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers import entity_platform
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_registry import async_entries_for_device
from homeassistant.util import Throttle
import homeassistant.util.color as color_util

from .const import (
    ATTR_IDENTIFIERS,
    ATTR_MANUFACTURER,
    ATTR_MODEL,
    CONF_AUTOMATIC_ADD,
    CONF_EFFECT_SPEED,
    DEFAULT_EFFECT_SPEED,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SIGNAL_ADD_DEVICE,
    SIGNAL_REMOVE_DEVICE,
)

_LOGGER = logging.getLogger(__name__)

CONF_COLORS = "colors"
CONF_SPEED_PCT = "speed_pct"
CONF_TRANSITION = "transition"
CONF_CUSTOM_EFFECT = "custom_effect"

SUPPORT_FLUX_LED = SUPPORT_BRIGHTNESS | SUPPORT_EFFECT | SUPPORT_COLOR

MODE_RGB = "rgb"
MODE_RGBW = "rgbw"
MODE_RGBCW = "rgbcw"
MODE_RGBWW = "rgbww"

# This mode enables white value to be controlled by brightness.
# RGB value is ignored when this mode is specified.
MODE_WHITE = "w"

# Constant color temp values for 2 flux_led special modes
# Warm-white and Cool-white modes
COLOR_TEMP_WARM_VS_COLD_WHITE_CUT_OFF = 285

# List of supported effects which aren't already declared in LIGHT
EFFECT_RED_FADE = "red_fade"
EFFECT_GREEN_FADE = "green_fade"
EFFECT_BLUE_FADE = "blue_fade"
EFFECT_YELLOW_FADE = "yellow_fade"
EFFECT_CYAN_FADE = "cyan_fade"
EFFECT_PURPLE_FADE = "purple_fade"
EFFECT_WHITE_FADE = "white_fade"
EFFECT_RED_GREEN_CROSS_FADE = "rg_cross_fade"
EFFECT_RED_BLUE_CROSS_FADE = "rb_cross_fade"
EFFECT_GREEN_BLUE_CROSS_FADE = "gb_cross_fade"
EFFECT_COLORSTROBE = "colorstrobe"
EFFECT_RED_STROBE = "red_strobe"
EFFECT_GREEN_STROBE = "green_strobe"
EFFECT_BLUE_STROBE = "blue_strobe"
EFFECT_YELLOW_STROBE = "yellow_strobe"
EFFECT_CYAN_STROBE = "cyan_strobe"
EFFECT_PURPLE_STROBE = "purple_strobe"
EFFECT_WHITE_STROBE = "white_strobe"
EFFECT_COLORJUMP = "colorjump"
EFFECT_CUSTOM = "custom"

EFFECT_MAP = {
    EFFECT_COLORLOOP: 0x25,
    EFFECT_RED_FADE: 0x26,
    EFFECT_GREEN_FADE: 0x27,
    EFFECT_BLUE_FADE: 0x28,
    EFFECT_YELLOW_FADE: 0x29,
    EFFECT_CYAN_FADE: 0x2A,
    EFFECT_PURPLE_FADE: 0x2B,
    EFFECT_WHITE_FADE: 0x2C,
    EFFECT_RED_GREEN_CROSS_FADE: 0x2D,
    EFFECT_RED_BLUE_CROSS_FADE: 0x2E,
    EFFECT_GREEN_BLUE_CROSS_FADE: 0x2F,
    EFFECT_COLORSTROBE: 0x30,
    EFFECT_RED_STROBE: 0x31,
    EFFECT_GREEN_STROBE: 0x32,
    EFFECT_BLUE_STROBE: 0x33,
    EFFECT_YELLOW_STROBE: 0x34,
    EFFECT_CYAN_STROBE: 0x35,
    EFFECT_PURPLE_STROBE: 0x36,
    EFFECT_WHITE_STROBE: 0x37,
    EFFECT_COLORJUMP: 0x38,
}
EFFECT_CUSTOM_CODE = 0x60

TRANSITION_GRADUAL = "gradual"
TRANSITION_JUMP = "jump"
TRANSITION_STROBE = "strobe"

FLUX_EFFECT_LIST = sorted(EFFECT_MAP) + [EFFECT_RANDOM]

SERVICE_CUSTOM_EFFECT = "set_custom_effect"

CUSTOM_EFFECT_SCHEMA = {
    vol.Required(CONF_COLORS): vol.All(
        cv.ensure_list,
        vol.Length(min=1, max=16),
        [vol.All(vol.ExactSequence((cv.byte, cv.byte, cv.byte)), vol.Coerce(tuple))],
    ),
    vol.Optional(CONF_SPEED_PCT, default=50): vol.All(
        vol.Range(min=0, max=100), vol.Coerce(int)
    ),
    vol.Optional(CONF_TRANSITION, default=TRANSITION_GRADUAL): vol.All(
        cv.string, vol.In([TRANSITION_GRADUAL, TRANSITION_JUMP, TRANSITION_STROBE])
    ),
}

DEVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(ATTR_MODE, default=MODE_RGBW): vol.All(
            cv.string, vol.In([MODE_RGBW, MODE_RGBWW, MODE_RGBCW, MODE_RGB, MODE_WHITE])
        ),
        vol.Optional(CONF_PROTOCOL): vol.All(cv.string, vol.In(["ledenet"])),
        vol.Optional(CONF_CUSTOM_EFFECT): CUSTOM_EFFECT_SCHEMA,
    }
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_DEVICES, default={}): {cv.string: DEVICE_SCHEMA},
        vol.Optional(CONF_AUTOMATIC_ADD, default=False): cv.boolean,
    }
)


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the platform and manage importing from YAML."""
    automatic_add = config["automatic_add"]
    devices = {}

    for import_host, import_item in config["devices"].items():
        import_name = import_host
        if import_item:
            import_name = import_item.get("name", import_host)

        devices[import_host.replace(".", "_")] = {
            CONF_NAME: import_name,
            CONF_HOST: import_host,
        }

    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_AUTOMATIC_ADD: automatic_add,
            CONF_DEVICES: devices,
        },
    )


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the Flux lights."""

    async def async_new_lights(bulbs: dict):
        """Add new bulbs when they are found or configured."""

        lights = []

        for bulb_id, bulb_details in bulbs.items():
            effect_speed = entry.options.get(bulb_id, {}).get(
                CONF_EFFECT_SPEED,
                entry.options.get("global", {}).get(
                    CONF_EFFECT_SPEED, DEFAULT_EFFECT_SPEED
                ),
            )

            host = bulb_details[CONF_HOST]
            try:
                bulb = await hass.async_add_executor_job(WifiLedBulb, host)
            except BrokenPipeError as error:
                raise PlatformNotReady(error) from error

            lights.append(
                FluxLight(
                    unique_id=bulb_id,
                    device=bulb_details,
                    effect_speed=effect_speed,
                    bulb=bulb,
                )
            )

        async_add_entities(lights, True)

    await async_new_lights(entry.data[CONF_DEVICES])

    async_dispatcher_connect(hass, SIGNAL_ADD_DEVICE, async_new_lights)

    # register custom_effect service
    platform = entity_platform.current_platform.get()

    platform.async_register_entity_service(
        SERVICE_CUSTOM_EFFECT,
        CUSTOM_EFFECT_SCHEMA,
        "set_custom_effect",
    )


class FluxLight(LightEntity):
    """Represents a Flux Light entity."""

    def __init__(self, unique_id: str, device: dict, effect_speed: int, bulb):
        """Initialize the Flux light entity."""
        self._name = device[CONF_NAME]
        self._unique_id = unique_id
        self._icon = "mdi:lightbulb"
        self._attrs = {}
        self._state = None
        self._brightness = None
        self._hs_color = None
        self._white_value = None
        self._current_effect = None
        self._last_brightness = None
        self._last_hs_color = None
        self._ip_address = device[CONF_HOST]
        self._effect_speed = effect_speed
        self._mode = None
        self._get_rgbw = None
        self._get_rgb = None
        self._bulb = bulb

    async def async_remove_light(self, device: dict):
        """Remove a bulb device when it is removed from options."""

        bulb_id = device["device_id"]

        if self._unique_id != bulb_id:
            return

        entity_registry = await self.hass.helpers.entity_registry.async_get_registry()
        entity_entry = entity_registry.async_get(self.entity_id)

        device_registry = await self.hass.helpers.device_registry.async_get_registry()
        device_entry = device_registry.async_get(entity_entry.device_id)

        if (
            len(
                async_entries_for_device(
                    entity_registry,
                    entity_entry.device_id,
                    include_disabled_entities=True,
                )
            )
            == 1
        ):
            # If only this entity exists on this device, remove the device.
            device_registry.async_remove_device(device_entry.id)

        entity_registry.async_remove(self.entity_id)

    async def async_added_to_hass(self):
        """Run when the entity is about to be added to hass."""
        await super().async_added_to_hass()

        async_dispatcher_connect(
            self.hass, SIGNAL_REMOVE_DEVICE, self.async_remove_light
        )

    def update_bulb_info(self):
        """Update the bulb information."""
        self._bulb.update_state()
        self._get_rgbw = self._bulb.getRgbw()
        self._get_rgb = self._bulb.getRgb()

    @Throttle(timedelta(seconds=DEFAULT_SCAN_INTERVAL))
    def update(self):
        """Fetch the data from this light bulb."""

        try:
            self.update_bulb_info()
        except BrokenPipeError as error:
            _LOGGER.warning("Error updating flux_led: %s", error)
            return

        if self._bulb.mode == "ww":
            self._mode = MODE_WHITE
        elif self._bulb.rgbwcapable and not self._bulb.rgbwprotocol:
            self._mode = MODE_RGBW
        else:
            self._mode = MODE_RGB

        if self._mode == MODE_RGBCW:
            white_temp = self.temperature_cw()
            self._white_value = (
                white_temp[0] if white_temp[0] > white_temp[1] else white_temp[1]
            )

            if white_temp[0] or white_temp[1]:
                self._brightness = 0

        elif self._mode == MODE_RGBWW:
            self._white_value = self.temperature_ww()

            if self._bulb.mode == "ww":
                self._brightness = 0
        else:
            self._white_value = self._get_rgbw[3]

            if self._mode == MODE_WHITE:
                self._brightness = self._white_value
            else:
                self._brightness = self._bulb.brightness

        self._hs_color = color_util.color_RGB_to_hs(*self._get_rgb)

        self._current_effect = self._bulb.raw_state[3]

        if self._bulb.is_on and self._brightness > 0:
            self._state = True
        else:
            self._state = False

        if self._state:
            self._last_brightness = self._brightness
            self._last_hs_color = self._hs_color

    @property
    def unique_id(self):
        """Return the unique ID of the light."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the light."""
        return self._name

    @property
    def is_on(self):
        """Return true if the light is on."""
        return self._state

    @property
    def brightness(self):
        """Return the brightness of the light."""
        return self._brightness

    @property
    def hs_color(self):
        """Return the color property."""
        return self._hs_color

    @property
    def white_value(self):
        """Return the white value of this light."""
        return self._white_value

    def temperature_cw(self):
        """Return the cold white temperature."""
        rgbww = self._bulb.getRgbww()
        return [rgbww[4], rgbww[3]]

    def temperature_ww(self):
        """Return the warm white temperature."""
        rgbww = self._bulb.getRgbww()
        return rgbww[3]

    @property
    def supported_features(self):
        """Return the supported features for this light."""
        if self._mode == MODE_RGBW or self._mode == MODE_RGBCW:
            return SUPPORT_FLUX_LED | SUPPORT_WHITE_VALUE | SUPPORT_COLOR_TEMP
        if self._mode == MODE_RGBWW:
            return SUPPORT_FLUX_LED | SUPPORT_WHITE_VALUE
        return SUPPORT_FLUX_LED

    @property
    def effect_list(self):
        """Return the list of supported effects."""
        return FLUX_EFFECT_LIST + [EFFECT_CUSTOM]

    @property
    def effect(self):
        """Return the current effect."""
        current_mode = self._current_effect

        if current_mode == EFFECT_CUSTOM_CODE:
            return EFFECT_CUSTOM

        for effect, code in EFFECT_MAP.items():
            if current_mode == code:
                return effect

        return None

    @property
    def device_state_attributes(self):
        """Return the attributes."""
        self._attrs["ip_address"] = self._ip_address

        return self._attrs

    @property
    def device_info(self):
        """Return the device information."""
        device_name = "FluxLED/Magic Home"
        device_model = "LED Lights"

        return {
            ATTR_IDENTIFIERS: {(DOMAIN, self._unique_id)},
            ATTR_NAME: self._name,
            ATTR_MANUFACTURER: device_name,
            ATTR_MODEL: device_model,
        }

    def turn_on(self, **kwargs):
        """Turn on the light."""

        rgb = None
        hs_color = kwargs.get(ATTR_HS_COLOR)

        if hs_color:
            rgb = color_util.color_hs_to_RGB(*hs_color)

        brightness = kwargs.get(ATTR_BRIGHTNESS)
        effect = kwargs.get(ATTR_EFFECT)
        white = kwargs.get(ATTR_WHITE_VALUE)
        color_temp = kwargs.get(ATTR_COLOR_TEMP)

        # Handle special modes
        if color_temp is not None:
            if white is None:
                white = self.white_value if self.white_value > 0 else 255

            if self._mode == MODE_RGBCW:
                # 153 - 500 color temp in mired

                # Map mired range from light input to kelvin range for bulb
                mired_min = 500
                mired_max = 153
                kelvin_min = 2700
                kelvin_max = 6500

                # Map mired to kelvin specifically for the setWhiteTemperature since it wants 2700~6500 kelvin and
                # we have 153~500 mired as an input which doesn't match up the way we want
                color_temp_kelvin = (color_temp - mired_min) / (
                    mired_max - mired_min
                ) * (kelvin_max - kelvin_min) + kelvin_min

                color_temp_kelvin = max(color_temp_kelvin - 2700, 0)
                warm = 255 * (1 - (color_temp_kelvin / 3800))
                cold = min(255 * color_temp_kelvin / 3800, 255)
                warm *= white / 255  # White controls brightness
                cold *= white / 255

                if (
                    warm > cold
                ):  # Warm side will activate both cold and warm leds at same rate (much brighter warm mode)
                    cold = warm

                self._bulb.setRgbw(w=warm, w2=cold)
                return

            if brightness is None:
                brightness = self.brightness
            if color_temp > COLOR_TEMP_WARM_VS_COLD_WHITE_CUT_OFF:
                self._bulb.setRgbw(w=brightness)
            else:
                self._bulb.setRgbw(w2=brightness)
            return

        if white is not None:
            if self._mode == MODE_RGBCW:
                current_temp = self.temperature_cw()
                if not current_temp[0] and not current_temp[1]:
                    current_temp[0] = 255
                    current_temp[1] = 255
                current_temp[0] *= white / 255
                current_temp[1] *= white / 255
                self._bulb.setRgbw(w=current_temp[1], w2=current_temp[0])
                return
            if self._mode == MODE_RGBWW:
                self._bulb.setWarmWhite255(white)
                return

        if effect == EFFECT_RANDOM:
            color_red = random.randint(0, 255)
            color_green = random.randint(0, 255)
            color_blue = random.randint(0, 255)

            self._bulb.setRgbw(
                r=color_red,
                g=color_green,
                b=color_blue,
            )

            self._hs_color = color_util.color_RGB_to_hs(
                color_red,
                color_green,
                color_blue,
            )

            return

        if effect in EFFECT_MAP:
            self._current_effect = effect
            self._bulb.setPresetPattern(EFFECT_MAP[effect], self._effect_speed)
            return

        if not brightness and not rgb and not self._state:
            self._state = True
            self._bulb.turnOn()
            return

        if not brightness:
            brightness = self._last_brightness

        self._brightness = brightness

        if not rgb and self._last_hs_color:
            rgb = color_util.color_hs_to_RGB(*self._last_hs_color)

        self._hs_color = color_util.color_RGB_to_hs(*tuple(rgb))

        if not white and self._mode == MODE_RGBW:
            white = self.white_value

        self._state = True
        self._hs_color = color_util.color_RGB_to_hs(*tuple(rgb))

        if self._mode == MODE_WHITE:
            self._bulb.setRgbw(0, 0, 0, w=brightness)

        elif self._mode == MODE_RGBW:
            self._bulb.setRgbw(*tuple(rgb), w=white, brightness=brightness)

        else:
            self._bulb.setRgb(*tuple(rgb), brightness=brightness)

    def turn_off(self, **kwargs):
        """Turn off the light."""

        self._last_brightness = self.brightness
        self._last_hs_color = self.hs_color

        self._state = False

        self._bulb.turnOff()

    def set_custom_effect(self, colors: list, speed_pct: int, transition: str):
        """Define custom service to set a custom effect on the lights."""

        if not self.is_on:
            self.turn_on()

        self._bulb.setCustomPattern(colors, speed_pct, transition)

        self._state = True
