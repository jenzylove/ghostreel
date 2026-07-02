"""App configuration.

Loads .env into the process environment so the Genblaze SDKs (which read os.environ
directly) and our typed Settings object both see the same values.
"""
from __future__ import annotations

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Populate os.environ from .env BEFORE the SDKs are imported/used.
load_dotenv()

# Selectable ElevenLabs voices (Phase 4 control). The default is the one proven in the spike;
# the others are long-standing public ElevenLabs voice IDs.
VOICES: dict[str, str] = {
    "George (default)": "JBFqnCBsd6RMkjVDRZzb",
    "Rachel": "21m00Tcm4TlvDq8ikWAM",
    "Adam": "pNInz6obpgDQGcFmaJgB",
    "Antoni": "ErXwobaYiN019PkySvjV",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    gemini_api_key: str = ""
    b2_key_id: str = ""
    b2_app_key: str = ""
    b2_bucket_name: str = ""
    eleven_api_key: str = ""

    image_model: str = "imagen-4.0-generate-001"
    chat_model: str = "gemini-2.5-flash"
    tts_model: str = "eleven_v3"
    voice_id: str = "JBFqnCBsd6RMkjVDRZzb"
    asset_prefix: str = "ghostreel"
    segment_count: int = 6

    # Phase 2 evaluate-retry
    qa_enabled: bool = True
    qa_model: str = "gemini-2.5-flash"
    qa_max_attempts: int = 2

    def missing_keys(self) -> list[str]:
        required = {
            "GEMINI_API_KEY": self.gemini_api_key,
            "B2_KEY_ID": self.b2_key_id,
            "B2_APP_KEY": self.b2_app_key,
            "B2_BUCKET_NAME": self.b2_bucket_name,
            "ELEVEN_API_KEY": self.eleven_api_key,
        }
        return [name for name, value in required.items() if not value]


settings = Settings()
