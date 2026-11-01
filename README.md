# Assist Failover

Run Assist's speech engines on a fast machine, and keep your voice control working when that machine is off.

A Raspberry Pi can run Whisper and Piper, but slowly. A desktop or home server is far quicker, but it's the machine most likely to be asleep or rebooting. Home Assistant's voice pipelines take exactly one speech-to-text engine and one text-to-speech engine, with no fallback, so moving them to a faster machine normally means voice fails whenever that machine is away.

Assist Failover adds one speech-to-text entity and one text-to-speech entity to use in your pipeline. Each one tries your primary engine, and switches to your backup when the primary can't answer.

## How it switches

| Situation | What happens | Extra wait |
|---|---|---|
| Primary is up | The primary answers | Almost none |
| Primary's last heartbeat failed | Goes straight to the backup | None |
| Primary went away since the last heartbeat | A quick check fails, then the backup | Up to 1 s |
| Primary accepts the request but hangs | Timeout, then the backup | Your primary timeout, 8 s by default |
| Primary returns an error or no audio | The backup, with the same audio or text | Time already spent |

A Wyoming primary gets a heartbeat every 10 seconds. It asks the service to describe itself, so an open port with a dead engine behind it counts as down. It returns to the primary by itself on the next good heartbeat.

Any `stt` or `tts` entity can be used, but only Wyoming primaries get a heartbeat and a status sensor. Others are found to be down by trying them.

## Measured

One Raspberry Pi 4 against one Ryzen 3950X desktop, CPU only, on the same network, through Home Assistant 2026.10.4. These used generated phrases, so expect real voices to take longer to transcribe.

| Engine | Pi 4 | Desktop |
|---|---|---|
| Whisper, a 1.6 s phrase | 2.4 s (`tiny-int8`) | 0.5 s (`base-int8`) |
| Piper, first audio of a short reply | 1.3 s | 0.2 s |
| Piper, first audio of one long sentence | 2.4 s | 0.35 s |

## Install

1. In HACS, open the menu, choose **Custom repositories**, and add this repository as an **Integration**.
2. Download **Assist Failover**, then restart Home Assistant.
3. Add your engines. For Wyoming, that's one **Wyoming Protocol** entry each for the fast machine and the backup. [`examples/docker-compose.yml`](examples/docker-compose.yml) starts Whisper and Piper on a machine with Docker.
4. Go to **Settings > Devices & services > Add integration > Assist Failover**, and choose a primary and backup for speech to text, text to speech, or both.
5. Go to **Settings > Voice assistants**, open your assistant, and choose **Assist Failover STT** and **Assist Failover TTS** as its engines.

Use the same voice on both text-to-speech engines, or replies will change voice when it switches. Pick your assistant's voice by the name both engines know, such as `en_US-lessac-medium`.

## What it adds

| Entity | What it shows |
|---|---|
| `stt.assist_failover_stt` | `last_engine`, `last_seconds` and `last_failures` from the latest request |
| `tts.assist_failover_tts` | The same |
| `binary_sensor.assist_failover_stt_primary` | Whether the speech-to-text primary answered its latest heartbeat |
| `binary_sensor.assist_failover_tts_primary` | The same for text to speech |

Each switch logs a warning naming the engine and the reason.

## Limits

- **A reply can't move once it's playing.** If the primary dies after audio has started, the reply stops short. The next one goes to the backup.
- **Speech to text waits for the whole clip.** That costs Whisper nothing, because it works on a complete clip anyway, but an engine that really transcribes as you talk loses that head start.
- **Text-to-speech caching.** Home Assistant caches replies under the failover entity, whichever engine made them. That's harmless if both use the same voice.
- **A slow backup is still slow.** This keeps voice working; it doesn't make the backup any faster.
- **Language lists are combined.** If only one engine supports a language, requests in that language fail when that engine is down, or go to the wrong one first and fail over.

## Testing

`tests/` covers the switching logic with no Home Assistant needed (`python -m unittest discover -s tests -t .`). The entities and config flow have no automated tests; they've been tested by hand on one Home Assistant 2026.10.4 install. [`docs/self-hosted-voice-notes.md`](docs/self-hosted-voice-notes.md) records how, and what else was learned along the way.

## License

MIT
