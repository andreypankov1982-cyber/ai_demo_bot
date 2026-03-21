from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,  # ← Важно: игнорируем регистр!
        extra="ignore"
    )

    tg_bot_token: str = Field(...)
    openai_api_key: str = Field(...)
    owner_contact: int = Field(...)

    gpt_model: str = Field(default="gpt-4o-mini")
    gpt_temperature: float = Field(default=0.3, ge=0, le=2)
    gpt_max_tokens: int = Field(default=250, ge=1, le=4096)
    gpt_timeout: int = Field(default=40, ge=5, le=120)

    database_url: str = Field(default="sqlite+aiosqlite:///bot.db")
    rate_limit: int = Field(default=10, ge=1, le=100)

    @field_validator("tg_bot_token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        if not v or len(v) < 40:
            raise ValueError("Некорректный токен бота")
        return v

    @field_validator("openai_api_key")
    @classmethod
    def validate_openai(cls, v: str) -> str:
        if not v or not v.startswith("sk-"):
            raise ValueError("Некорректный ключ OpenAI")
        return v

    @field_validator("owner_contact")
    @classmethod
    def validate_owner(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("owner_contact должен быть > 0")
        return v


settings = Settings()

# Для совместимости со старым кодом
TG_BOT_TOKEN = settings.tg_bot_token
OPENAI_API_KEY = settings.openai_api_key
OWNER_CONTACT = settings.owner_contact