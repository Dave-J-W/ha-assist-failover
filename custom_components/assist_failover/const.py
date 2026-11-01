"""Constants for Assist Failover."""

DOMAIN = "assist_failover"

CONF_STT_PRIMARY = "stt_primary"
CONF_STT_BACKUP = "stt_backup"
CONF_TTS_PRIMARY = "tts_primary"
CONF_TTS_BACKUP = "tts_backup"
CONF_PRIMARY_TIMEOUT = "primary_timeout"

DEFAULT_PRIMARY_TIMEOUT = 8

# The heartbeat lets a voice turn skip a dead primary at once. Between beats a sleeping machine
# never answers, so a turn still asks first rather than waste the primary's timeout.
HEARTBEAT_SECONDS = 10
HEARTBEAT_TIMEOUT = 2.0
REACHABLE_TIMEOUT = 1.0

# The backup is the last resort, so it gets as long as a slow machine could need.
BACKUP_TIMEOUT = 60.0

KIND_STT = "stt"
KIND_TTS = "tts"
