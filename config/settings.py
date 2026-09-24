import os
import sys
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_TOML_PATH = BASE_DIR / "config.toml"


class BrowserConfig(BaseModel):
    default_brave_path: str = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    )
    flags: List[str] = Field(default_factory=list)


class SafetyConfig(BaseModel):
    max_daily_requests: int = 8
    max_weekly_requests: int = 50
    operating_hour_start: int = 9
    operating_hour_end: int = 19
    lock_timeout_seconds: int = 30
    user_lock_timeout_seconds: int = 10


class ScrapingConfig(BaseModel):
    default_max_pages: int = 5
    min_required_profiles: int = 10
    default_withdrawal_batch: int = 20


class QueriesConfig(BaseModel):
    role_keywords: str = '("Software Engineer" OR "SWE" OR "Backend" OR "Tech Lead")'


class GrowthConfig(BaseModel):
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    scraping: ScrapingConfig = Field(default_factory=ScrapingConfig)
    queries: QueriesConfig = Field(default_factory=QueriesConfig)


class ContentConfig(BaseModel):
    technical_topics: List[str] = Field(default_factory=list)


class SecretsConfig(BaseModel):
    openai_api_key: Optional[str] = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY"))
    http_proxy: Optional[str] = Field(default_factory=lambda: os.getenv("HTTP_PROXY"))


class Settings(BaseModel):
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    growth: GrowthConfig = Field(default_factory=GrowthConfig)
    content: ContentConfig = Field(default_factory=ContentConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)

    @classmethod
    def load(cls, toml_path: Path = CONFIG_TOML_PATH) -> "Settings":
        data = {}
        if toml_path.exists():
            with open(toml_path, "rb") as f:
                data = tomllib.load(f)
        return cls(**data)


# Global singleton instance
settings = Settings.load()