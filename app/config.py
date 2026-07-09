from urllib.parse import quote

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # RabbitMQ
    RABBITMQ_HOST: str = "91.98.64.92"
    RABBITMQ_PORT: int = 5672
    RABBITMQ_USER: str = "guest"
    RABBITMQ_PASS: str = "guest"
    RABBITMQ_VHOST: str = "default"
    RABBITMQ_QUEUE: str = "seven-claude"
    # Permite consumir várias msgs em paralelo — essencial para o debounce
    # Redis agrupar mensagens do mesmo lead que chegam em sequência rápida.
    RABBITMQ_PREFETCH: int = 20

    # Redis
    REDIS_HOST: str = "91.98.64.92"
    REDIS_PORT: int = 6380
    REDIS_PASSWORD: str = ""

    # Google Gemini
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.5-flash"
    # Modelo usado quando o primário esgota tentativas por sobrecarga (503/429).
    # Deve aceitar o mesmo contrato (function calling + system_instruction).
    GEMINI_FALLBACK_MODEL: str = "gemini-flash-latest"

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
    # Host usado para POST /v1/classattendance. O fluxo n8n aponta para o host v2
    # mesmo usando token v1. O script scripts/test_cloudgym.py tenta ambos e
    # deixa configurado aqui o que respondeu 2xx.
    CLOUDGYM_ATTENDANCE_BASE: str = "https://api.cloudgym.io"

    # Google Sheets
    GOOGLE_CREDENTIALS_JSON: str = ""
    GOOGLE_SHEET_ID: str = ""

    # Identidade do cliente (usada no prefixo das rotas e chaves Redis)
    CLIENT_SLUG: str = "seven"
    CLIENT_NAME: str = "Seven Academia"

    # App
    WEBHOOK_PATH: str = "/seven"
    # Base publica para servir imagens da pasta app/media via StaticFiles
    # (montado em /{CLIENT_SLUG}/media). Substitui os links antigos do Wix.
    MEDIA_BASE_URL: str = "https://webhook-whatsapp.strategicai.com.br/seven"
    DEBOUNCE_SECONDS: int = 30
    BLOCK_TTL_SECONDS: int | None = None  # deprecated: TTL agora é calculado dinamicamente até amanhã 08:00 SP

    # Alerta de atendimento humano (somente dígitos, DDI+DDD+numero)
    ALERT_PHONE: str = "5511989887525"

    # Whitelist de números: vazio = responde a todos
    ALLOWED_PHONES: str = ""

    # Blocklist de números: mensagens desses números são descartadas antes da fila
    BLOCKED_SENDER_PHONES: str = ""

    # Números que pulam o debounce (CSV de dígitos). Usado para números de teste
    # em que queremos resposta imediata.
    DEBOUNCE_BYPASS_PHONES: str = ""

    # SQLite
    SQLITE_PATH: str = "/data/seven.db"

    # Scheduler
    SCHEDULER_TZ: str = "America/Sao_Paulo"
    FOLLOWUP_DRY_RUN: int = 0

    # Alias necessario para sai_sync._binding_key (que usa PROJECT_SLUG como fallback)
    PROJECT_SLUG: str = "seven"

    # Sincronizacao com o Painel IA WhatsApp (SAI Comercial)
    SAI_BASE_URL: str = "https://comercial.strategicai.com.br"
    SAI_TENANT_SLUG: str = ""
    SAI_INGEST_SECRET: str = ""

    # Auto-registro do chatbot no catalogo do SAI.
    SAI_CHATBOT_SLUG: str = ""
    SAI_CHATBOT_NAME: str = ""
    SAI_CHATBOT_PUBLIC_URL: str = ""
    SAI_REGISTRATION_TOKEN: str = ""

    @property
    def allowed_phones_list(self) -> list[str]:
        return [p.strip() for p in self.ALLOWED_PHONES.split(",") if p.strip()]

    @property
    def blocked_sender_phones_set(self) -> set[str]:
        return {p.strip() for p in self.BLOCKED_SENDER_PHONES.split(",") if p.strip()}

    @property
    def debounce_bypass_phones_set(self) -> set[str]:
        return {p.strip() for p in self.DEBOUNCE_BYPASS_PHONES.split(",") if p.strip()}

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
