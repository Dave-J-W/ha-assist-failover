"""The failover speech-to-text entity."""

import logging

from homeassistant.components import stt

from .const import BACKUP_TIMEOUT, CONF_PRIMARY_TIMEOUT, CONF_STT_BACKUP, DEFAULT_PRIMARY_TIMEOUT, KIND_STT
from .logic import Attempt, first_answer
from .wyoming_direct import languages, transcribe

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    config = {**entry.data, **entry.options}
    if KIND_STT in entry.runtime_data and config.get(CONF_STT_BACKUP):
        async_add_entities([FailoverStt(entry, config)])


async def _replay(chunks):
    for chunk in chunks:
        yield chunk


class FailoverStt(stt.SpeechToTextEntity):
    """Buffers the clip so the backup can hear it too.

    Assist already waits for the end of speech before it needs a transcript, so this costs a
    batch engine like Whisper nothing. A truly streaming engine would lose its head start.
    """

    _attr_name = "Assist Failover STT"

    def __init__(self, entry, config):
        self._primary = entry.runtime_data[KIND_STT]
        self._backup = config[CONF_STT_BACKUP]
        self._timeout = config.get(CONF_PRIMARY_TIMEOUT, DEFAULT_PRIMARY_TIMEOUT)
        self._attr_unique_id = f"{entry.entry_id}-stt"
        self._attr_extra_state_attributes = {}

    def _entity(self, entity_id):
        return stt.async_get_speech_to_text_entity(self.hass, entity_id)

    def _entities(self):
        ids = [self._backup] if self._primary.direct else [self._primary.entity_id, self._backup]
        return [engine for engine in map(self._entity, ids) if engine is not None]

    @property
    def supported_languages(self):
        found = languages(self._primary.info, "asr")
        for engine in self._entities():
            found.update(engine.supported_languages)
        return sorted(found) or [self.hass.config.language]

    @property
    def supported_formats(self):
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self):
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self):
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self):
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self):
        return [stt.AudioChannels.CHANNEL_MONO]

    @property
    def audio_processing(self):
        engines = self._entities()
        return engines[0].audio_processing if engines else super().audio_processing

    def _entity_runner(self, entity_id, metadata, chunks):
        async def run():
            engine = self._entity(entity_id)
            if engine is None:
                raise LookupError(f"{entity_id} is not loaded")
            result = await engine.internal_async_process_audio_stream(metadata, _replay(chunks))
            if result.result != stt.SpeechResultState.SUCCESS:
                return None
            return result.text or ""

        return run

    def _primary_runner(self, metadata, chunks):
        if not self._primary.direct:
            return self._entity_runner(self._primary.entity_id, metadata, chunks)

        async def run():
            return await transcribe(*self._primary.address, metadata.language, chunks)

        return run

    async def async_process_audio_stream(self, metadata, stream):
        chunks = [chunk async for chunk in stream]
        outcome = await first_answer(
            [
                Attempt(self._primary.name, self._primary_runner(metadata, chunks), self._timeout, self._primary.reachable),
                Attempt(self._backup, self._entity_runner(self._backup, metadata, chunks), BACKUP_TIMEOUT),
            ]
        )
        if outcome.failures:
            _LOGGER.warning("Speech to text: %s", "; ".join(outcome.failures))
        self._attr_extra_state_attributes = {
            "last_engine": outcome.engine,
            "last_seconds": round(outcome.seconds, 2),
            "last_failures": outcome.failures,
        }
        self.async_write_ha_state()
        if outcome.engine is None:
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
        return stt.SpeechResult(outcome.value, stt.SpeechResultState.SUCCESS)
