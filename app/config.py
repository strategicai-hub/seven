from urllib.parse import quote

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # RabbitMQ
    RABBITMQ_HOST: str = "91.98.64.92"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_USER: str = "guest"
    RABBITMQ_PASS: str = "guest"
    RABBITMQ_VHOST: str = "default"
    RABBITMQ_QUEUE: str = "seven"

    # Redis
    REDIS_HOST: str = "91.98.64.92"
    REDIS_PORT: int = 6380
    REDIS_PASSWORD: str = ""

    # Google Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"

    # UAZAPI
    UAZAPI_BASE_URL: str = "https://strategicai.uazapi.com"
    UAZAPI_TOKEN: str = ""
    UAZAPI_INSTANCE: str = "seven"

    # CloudGym — v1 e v2 são usadas em endpoints diferentes
    CLOUDGYM_UNIT_ID: int = 2751
    CLOUDGYM_V1_BASE: str = "https://api.prod.cloudgym.io"
    CLOUDGYM_V1_BASIC: str = ""
    CLOUDGYM_V2_BASE: str = "https://api.cloudgym.io"
    CLOUDGYM_V2_USERNAME: str = ""
    CLOUDGYM_V2_PASSWORD: str = ""
    CLOUDGYM_PROXY: str = ""

    # Google Sheets
    GOOGLE_CREDENTIALS_JSON: str = ""
    GOOGLE_SHEET_ID: str = ""

    # Identidade do cliente (usada no prefixo das rotas e chaves Redis)
    CLIENT_SLUG: str = "seven"
    CLIENT_NAME: str = "Seven Academia"

    # App
    WEBHOOK_PATH: str = "/seven"
    DEBOUNCE_SECONDS: int = 30
    BLOCK_TTL_SECONDS: int = 3600

    # Alerta de atendimento humano (somente dígitos, DDI+DDD+numero)
    ALERT_PHONE: str = "5511989887525"

    # Whitelist de números: vazio = responde a todos
    ALLOWED_PHONES: str = ""

    # SQLite
    SQLITE_PATH: str = "/data/seven.db"

    # Scheduler
    SCHEDULER_TZ: str = "America/Sao_Paulo"
    FOLLOWUP_DRY_RUN: int = 0

    @property
    def allowed_phones_list(self) -> list[str]:
        return [p.strip() for p in self.ALLOWED_PHONES.split(",") if p.strip()]

    @property
    def rabbitmq_url(self) -> str:
        user = quote(self.RABBITMQ_USER, safe="")
        password = quote(self.RABBITMQ_PASS, safe="")
        vhost = quote(self.RABBITMQ_VHOST, safe="")
        return (
            f"amqp://{user}:{password}"
            f"@{self.RABBITMQ_HOST}:{self.RABBITMQ_PORT}/{vhost}"
        )

    @property
    def redis_url(self) -> str:
        if self.REDIS_PASSWORD:
            return f"redis://:{self.REDIS_PASSWORD}@{self.REDIS_HOST}:{self.REDIS_PORT}/0"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
