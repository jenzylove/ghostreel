"""App configuration.

Loads .env into the process environment so the Genblaze SDKs (which read os.environ
directly) and our typed Settings object both see the same values. Mirrors the env-var
setup proven in the WSL spike: GEMINI_API_KEY, B2_KEY_ID, B2_APP_KEY.
"""
from __future__ import annotations

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Populate os.environ from .env BEFORE anything imports the SDKs, so genblaze-google and
# genblaze-s3 (which read os.environ) pick up the same values our Settings validates.
load_dotenv()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Read from the environment by the providers/sink; loaded here too so /health can
    # confirm they're present before a job ever runs.
    gemini_api_key: str = ""
    b2_key_id: str = ""
    b2_app_key: str = ""
    b2_bucket_name: str = ""

    image_model: str = "imagen-4.0-generate-001"
    asset_prefix: str = "ghostreel"

    def missing_keys(self) -> list[str]:
        required = {
            "GEMINI_API_KEY": self.gemini_api_key,
            "B2_KEY_ID": self.b2_key_id,
            "B2_APP_KEY": self.b2_app_key,
            "B2_BUCKET_NAME": self.b2_bucket_name,
        }
        return [name for name, value in required.items() if not value]


settings = Settings()
