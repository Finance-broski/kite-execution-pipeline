"""Minimal settings loader so the pipeline runs standalone.

Reads KITE_API_KEY / KITE_API_SECRET from the environment (loads a local .env via
python-dotenv if available). Drop in your own config object if integrating this
into a larger project - callers only use `get_settings().kite_api_key/secret`.
"""
import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:                                            # noqa: BLE001
    pass


@dataclass(frozen=True)
class Settings:
    kite_api_key: str = ""
    kite_api_secret: str = ""


def get_settings() -> "Settings":
    return Settings(
        kite_api_key=os.environ.get("KITE_API_KEY", ""),
        kite_api_secret=os.environ.get("KITE_API_SECRET", ""),
    )
