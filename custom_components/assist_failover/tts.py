"""The failover text-to-speech entity."""

import logging

from homeassistant.components import tts
from homeassistant.components.tts.entity import TTSAudioRequest
from homeassistant.components.tts.helper import get_engine_instance
from homeassistant.exceptions import HomeAssistantError

from .const import BACKUP_TIMEOUT, CONF_PRIMARY_TIMEOUT, CONF_TTS_BACKUP, DEFAULT_PRIMARY_TIMEOUT, KIND_TTS
from .logic import Attempt, ReplayableText, first_answer, first_audio, options_for
from .wyoming_direct import languages, supports_streaming, synthesize, voices

_LOGGER = logging.getLogger(__name__)

# What a Wyoming server can be asked for, when it's the primary directly.
DIRECT_OPTIONS = [tts.ATTR_VOICE, "speaker"]


async def async_setup_entry(hass, entry, async_add_entities):
    config = {**entry.data, **entry.options}
    if KIND_TTS in entry.runtime_data and config.get(CONF_TTS_BACKUP):
        async_add_entities([FailoverTts(entry, config)])


class FailoverTts(tts.TextToSpeechEntity):
    """Hands over to the backup any time before real audio arrives.

    After that the reply is already playing, so a primary that dies mid-sentence cuts it short.
    """

    _attr_name = "Assist Failover TTS"

    def __init__(self, entry, config):
        self._primary = entry.runtime_data[KIND_TTS]
        self._backup = config[CONF_TTS_BACKUP]
        self._timeout = config.get(CONF_PRIMARY_TIMEOUT, DEFAULT_PRIMARY_TIMEOUT)
        self._attr_unique_id = f"{entry.entry_id}-tts"
        self._attr_extra_state_attributes = {}

    def _entity(self, entity_id):
        engine = get_engine_instance(self.hass, entity_id)
        return engine if isinstance(engine, tts.TextToSpeechEntity) else None

    def _entities(self):
        ids = [self._backup] if self._primary.direct else [self._primary.entity_id, self._backup]
        return [engine for engine in map(self._entity, ids) if engine is not None]

    @property
    def supported_languages(self):
        found = languages(self._primary.info, "tts")
        for engine in self._entities():
            found.update(engine.supported_languages)
        return sorted(found) or [self.hass.config.language]

    @property
    def default_language(self):
        found = languages(self._primary.info, "tts")
        if found:
            return sorted(found)[0]
        engines = self._entities()
        return engines[0].default_language if engines else self.hass.config.language

    @property
    def supported_options(self):
        found = set(DIRECT_OPTIONS) if self._primary.direct else set()
        for engine in self._entities():
            found.update(engine.supported_options or [])
        return sorted(found)

    @property
    def default_options(self):
        return None

    def async_supports_streaming_input(self):
        return True

    def async_get_supported_voices(self, language):
        if self._primary.direct and (found := voices(self._primary.info, language)):
            return [tts.Voice(voice_id=name, name=description) for name, description in found]
        for engine in self._entities():
            if found := engine.async_get_supported_voices(language):
                return found
        return None

    def _entity_runner(self, entity_id, request, text):
        async def run():
            engine = self._entity(entity_id)
            if engine is None:
                raise LookupError(f"{entity_id} is not loaded")
            response = await engine.internal_async_stream_tts_audio(
                TTSAudioRequest(
                    request.language,
                    options_for(request.options, engine.supported_options),
                    text.reader(),
                )
            )
            started = await first_audio(response.data_gen)
            if started is None:
                return None
            return response.extension, started[0], started[1], entity_id

        return run

    def _primary_runner(self, request, text):
        if not self._primary.direct:
            return self._entity_runner(self._primary.entity_id, request, text)

        async def run():
            voice = None
            if name := request.options.get(tts.ATTR_VOICE):
                voice = {"name": name}
                if speaker := request.options.get("speaker"):
                    voice["speaker"] = speaker
            started = await first_audio(
                synthesize(
                    *self._primary.address,
                    text.reader(),
                    voice,
                    supports_streaming(self._primary.info),
                )
            )
            if started is None:
                return None
            return "wav", started[0], started[1], self._primary.name

        return run

    async def async_stream_tts_audio(self, request):
        text = ReplayableText(request.message_gen)
        await text.wait_for_text()
        outcome = await first_answer(
            [
                Attempt(self._primary.name, self._primary_runner(request, text), self._timeout, self._primary.reachable),
                Attempt(self._backup, self._entity_runner(self._backup, request, text), BACKUP_TIMEOUT),
            ]
        )
        if outcome.failures:
            _LOGGER.warning("Text to speech: %s", "; ".join(outcome.failures))
        self._attr_extra_state_attributes = {
            "last_engine": outcome.engine,
            "last_seconds": round(outcome.seconds, 2),
            "last_failures": outcome.failures,
        }
        self.async_write_ha_state()
        if outcome.engine is None:
            raise HomeAssistantError(f"No engine could speak: {'; '.join(outcome.failures)}")

        extension, first, rest, engine_name = outcome.value

        async def data_gen():
            for chunk in first:
                yield chunk
            try:
                async for chunk in rest:
                    yield chunk
            except Exception as err:  # noqa: BLE001 - keep what has already been spoken
                _LOGGER.warning("Text to speech: %s failed mid-reply: %s", engine_name, err)

        return tts.TTSAudioResponse(extension, data_gen())
