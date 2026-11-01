"""The failover text-to-speech entity."""

import logging

from homeassistant.components import tts
from homeassistant.components.tts.entity import TTSAudioRequest
from homeassistant.components.tts.helper import get_engine_instance
from homeassistant.exceptions import HomeAssistantError

from .const import (
    BACKUP_TIMEOUT,
    CONF_PRIMARY_TIMEOUT,
    CONF_TTS_BACKUP,
    CONF_TTS_PRIMARY,
    DEFAULT_PRIMARY_TIMEOUT,
    KIND_TTS,
)
from .logic import Attempt, ReplayableText, first_answer, first_audio, options_for

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    config = {**entry.data, **entry.options}
    if config.get(CONF_TTS_PRIMARY) and config.get(CONF_TTS_BACKUP):
        async_add_entities([FailoverTts(entry, config)])


class FailoverTts(tts.TextToSpeechEntity):
    """Hands over to the backup any time before real audio arrives.

    After that the reply is already playing, so a primary that dies mid-sentence cuts it short.
    """

    _attr_name = "Assist Failover TTS"

    def __init__(self, entry, config):
        self._primary = config[CONF_TTS_PRIMARY]
        self._backup = config[CONF_TTS_BACKUP]
        self._timeout = config.get(CONF_PRIMARY_TIMEOUT, DEFAULT_PRIMARY_TIMEOUT)
        self._heartbeat = entry.runtime_data[KIND_TTS]
        self._attr_unique_id = f"{entry.entry_id}-tts"
        self._attr_extra_state_attributes = {}

    def _engine(self, entity_id):
        engine = get_engine_instance(self.hass, entity_id)
        return engine if isinstance(engine, tts.TextToSpeechEntity) else None

    def _engines(self):
        found = (self._engine(entity_id) for entity_id in (self._primary, self._backup))
        return [engine for engine in found if engine is not None]

    @property
    def supported_languages(self):
        languages = {lang for engine in self._engines() for lang in engine.supported_languages}
        return sorted(languages) or [self.hass.config.language]

    @property
    def default_language(self):
        engines = self._engines()
        return engines[0].default_language if engines else self.hass.config.language

    @property
    def supported_options(self):
        return sorted({opt for engine in self._engines() for opt in engine.supported_options or []})

    @property
    def default_options(self):
        return None

    def async_supports_streaming_input(self):
        return True

    def async_get_supported_voices(self, language):
        for engine in self._engines():
            if voices := engine.async_get_supported_voices(language):
                return voices
        return None

    def _runner(self, entity_id, request, text):
        async def run():
            engine = self._engine(entity_id)
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

    async def async_stream_tts_audio(self, request):
        text = ReplayableText(request.message_gen)
        await text.wait_for_text()
        outcome = await first_answer(
            [
                Attempt(self._primary, self._runner(self._primary, request, text), self._timeout, self._heartbeat.reachable),
                Attempt(self._backup, self._runner(self._backup, request, text), BACKUP_TIMEOUT),
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

        extension, first, rest, engine_id = outcome.value

        async def data_gen():
            for chunk in first:
                yield chunk
            try:
                async for chunk in rest:
                    yield chunk
            except Exception as err:  # noqa: BLE001 - keep what has already been spoken
                _LOGGER.warning("Text to speech: %s failed mid-reply: %s", engine_id, err)

        return tts.TTSAudioResponse(extension, data_gen())
