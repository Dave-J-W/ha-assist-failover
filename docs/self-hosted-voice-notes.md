# Notes on self-hosted voice in Home Assistant

What was learned building this, written so the next person or agent doesn't have to rediscover it. Everything was measured or read on Home Assistant 2026.10.4 unless it says otherwise.

## Finding what's slow

- **Read the pipeline's own timings first.** WebSocket `assist_pipeline/pipeline/list`, then `assist_pipeline/pipeline_debug/list` and `/get` give every stage's timestamp for recent runs, plus the transcript. Time from `stt-vad-end` to `stt-end` is the speech-to-text engine.
- **`tts-end` is not when speech finished.** It arrives almost at once, because audio is generated when the player asks for it. Time text to speech separately: `POST /api/tts_get_url`, then fetch the URL and time the first byte. Use a new phrase each time, because replies are cached.
- **`POST /api/stt/<entity_id>`** tests one speech-to-text engine with a WAV file and no voice pipeline. Header: `X-Speech-Content: format=wav; codec=pcm; sample_rate=16000; bit_rate=16; channel=1; language=en`.
- **Average with care.** The same word took 2.1 s and 6.5 s on a Pi's Whisper. The cause wasn't found. Look at the range, not one run.

## Choosing engines

- **Whisper `base-int8` beat `small-int8` on a desktop CPU for this job.** About 0.5 s against 1.2-1.4 s, and they transcribed the clean test phrases the same. Clean generated speech says little about a real room, so try real voices before settling.
- **`--beam-size 1` and `--cpu-threads` matter.** On a 16-core desktop, the defaults took about 1.8 s with `small-int8`; beam size 1 with 8 threads took 1.4 s, and 16 threads took 1.2 s.
- **A lower Piper voice quality is a poor trade if you have a second machine.** On a Pi 4, `low` saved about a third, while moving `medium` to a desktop saved over 80%.
- **The built-in conversation agent isn't the delay.** Intent handling took under 0.2 s. Speech to text and text to speech were almost all of the measurable time.
- **Piper is slow once after it starts.** The first reply from a restarted container took 1.2 s, then about 0.3 s, because the voice loads on first use.

## Assist's rules that shaped this

- **A pipeline has one engine of each kind, with no fallback.** That's the reason this integration exists. An automation can't swap engines either: pipelines change only through the WebSocket API, not a service.
- **Changing a pipeline needs every field sent back.** `assist_pipeline/pipeline/update` takes the whole pipeline, so list it, change one field and send it all, minus `id` (which goes in as `pipeline_id`).
- **A new custom integration needs a restart before its config flow exists.** Until then, starting the flow answers `Invalid handler specified`.
- **Nothing is exposed to voice by default in some areas.** "What's the weather" answered "no weather is exposed" until a `weather` entity was exposed to the conversation assistant (`homeassistant/expose_entity`). Sensors named for temperature don't help. Meanwhile "temperature outside" still failed: the built-in agent reads "outside" as an area, and looks for a thermostat in it.

## Wrapping a speech engine

- **`internal_async_process_audio_stream` and `internal_async_stream_tts_audio` are the calls to make**, because Home Assistant uses them itself and they keep the engine's "last used" state. Find an engine with `stt.async_get_speech_to_text_entity` and `tts.helper.get_engine_instance`.
- **Don't list `wyoming` in `after_dependencies`.** That was this integration's first bug. With the primary's service stopped during a Home Assistant restart, its Wyoming entry ended in `setup_error`, and this integration, which was waiting behind Wyoming, stayed `not_loaded`. So the failover failed in the exact case it exists for, and voice was down with the backup healthy. Only testing a restart with the primary down found it. Look engines up when they're needed instead, since the registry knows about them even while they're down.
- **A Wyoming entry in `setup_error` isn't retried.** It stays down after its service returns, until something reloads it. This integration checks every 30 s and reloads an engine's entry once its service answers.
- **Audio can only be read once**, so a fallback needs its own copy. For speech to text, buffering the clip costs nothing with a batch engine.
- **Text can stream, so don't buffer it.** A streaming agent's first sentence would have to wait for its last. `ReplayableText` gives the primary text as it arrives, and lets a backup replay it.
- **Start the primary's timer after the first text arrives**, or a slow conversation agent gets blamed on the speech engine.
- **A Wyoming stream's first chunk is only a WAV header.** It's sent when audio starts, before any speech. Its reader also ends quietly on a dropped connection rather than raising. So "got a first chunk" doesn't prove anything; read past the header.
- **`supported_languages` is matched exactly**, so `en` and `en_US` are different. Whisper reports `en`; Piper reports `en_US`.
- **Options differ between engines.** Filter them per engine, and don't list `preferred_format` and friends as supported unless you handle them, or Home Assistant stops converting audio for you.
- **An open port isn't a working engine.** Send a Wyoming `describe` and require an `info` reply: `{"type": "describe"}` plus a newline.

## Running the fast machine

- **Check power settings.** A desktop set to sleep takes the primary with it. Failover covers that, but you'd be on the slow engine more than you think.
- **Give it a fixed address.** The Wyoming entry stores the host.
- **On Windows with Docker Desktop**, a published port was reachable from the LAN with no added firewall rule on this one machine. Test yours from the Home Assistant host, for example `nc -z -w3 <address> 10300`.
- **Keep model caches in a named volume** rather than a mounted drive, so a problem with the drive doesn't take voice down.
- **Pin image versions.** Use the same Piper voice as the backup, and `rhasspy/wyoming-piper:2.5.2` downloads it on first start.

## Testing it

The checks that proved something:

1. Each engine alone through `POST /api/stt/...` and the TTS URL.
2. The failover entities with the primary up, checking `last_engine`.
3. Stop the primary's container, then request right away: this is the window before a heartbeat notices.
4. Wait over 10 s and request again: this should show no extra delay, and the binary sensor off.
5. Start the container, and check that it returns by itself.
6. `docker pause` the primary, then request right away. A paused container still accepts connections and then says nothing, so this is the real hang case: it should wait out the timeout, then use the backup. (`docker stop` only tests a refused connection.)
7. Stop the primary, restart Home Assistant, and check the backup works from boot and the primary returns by itself.
8. A real `assist_satellite.announce`, which goes through the pipeline's text to speech. `last_engine` shows who made it.
9. A real voice turn on the satellite. Generated speech doesn't stand in for this.

Results on one install, with a 1.6 s test clip and a short reply. Speech to text is the whole request; text to speech is the time to first byte:

| Case | Speech to text | Text to speech |
|---|---|---|
| Primary up | 0.57-0.59 s | 0.28-0.32 s |
| Just stopped, heartbeat hadn't noticed | 3.5 s, backup | 2.8 s, backup |
| Stopped, heartbeat knows | 2.3 s, backup | 1.4-1.7 s, backup |
| Just paused (hung) | 10.3 s, backup | 9.6 s, backup |
| Back after a stop or pause | Primary again within 8 s | Primary again within 8 s |

Not tested: a primary machine that's really off or asleep (only containers were stopped or paused), and a primary that dies partway through a reply.
