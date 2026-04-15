import json
import redis.asyncio as redis

from app.config import settings

_pool: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _pool
    if _pool is None:
        _pool = redis.from_url(settings.redis_url, decode_responses=True)
    return _pool


def _base_key(phone: str) -> str:
    return f"{phone}--seven"


# ---- bloqueio de agente ----

async def set_block(phone: str, ttl: int = settings.BLOCK_TTL_SECONDS) -> None:
    r = await get_redis()
    await r.set(f"{_base_key(phone)}:block", "1", ex=ttl)


async def is_blocked(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(f"{_base_key(phone)}:block") == 1


# ---- buffer de mensagens (debounce) ----

def _buffer_key(phone: str) -> str:
    return f"{_base_key(phone)}:buffer"


async def push_buffer(phone: str, text: str) -> int:
    r = await get_redis()
    return await r.rpush(_buffer_key(phone), text)


async def get_buffer(phone: str) -> list[str]:
    r = await get_redis()
    return await r.lrange(_buffer_key(phone), 0, -1)


async def delete_buffer(phone: str) -> None:
    r = await get_redis()
    await r.delete(_buffer_key(phone))


# ---- historico de chat (Gemini) ----

def _history_key(phone: str) -> str:
    return f"{_base_key(phone)}:history"


async def get_chat_history(phone: str) -> list[dict]:
    r = await get_redis()
    raw = await r.lrange(_history_key(phone), 0, -1)
    history = []
    for item in raw:
        entry = json.loads(item)
        if "type" in entry:
            role = "model" if entry["type"] == "ai" else "user"
            text = entry.get("data", {}).get("content", "")
            history.append({"role": role, "parts": [{"text": text}]})
        else:
            history.append(entry)
    return history


async def append_chat_history(phone: str, role: str, text: str) -> None:
    r = await get_redis()
    entry_type = "ai" if role == "model" else "human"
    entry = json.dumps({"type": entry_type, "data": {"content": text}}, ensure_ascii=False)
    await r.rpush(_history_key(phone), entry)
    await r.ltrim(_history_key(phone), -30, -1)  # 30 msgs (menor que aje por causa do prompt maior)


async def clear_chat_history(phone: str) -> None:
    r = await get_redis()
    await r.delete(_history_key(phone))


# ---- alerta de atendimento humano ----

async def set_alert_sent(phone: str, ttl: int = 3600) -> None:
    r = await get_redis()
    await r.set(f"{_base_key(phone)}:alert", "1", ex=ttl)


async def is_alert_sent(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(f"{_base_key(phone)}:alert") == 1


# ---- flag genérica com TTL (para deduplicação de follow-ups) ----

async def set_flag(key: str, ttl: int) -> None:
    r = await get_redis()
    await r.set(key, "1", ex=ttl)


async def has_flag(key: str) -> bool:
    r = await get_redis()
    return await r.exists(key) == 1


# ---- cache de token (CloudGym) ----

async def cache_get(key: str) -> str | None:
    r = await get_redis()
    return await r.get(key)


async def cache_set(key: str, value: str, ttl: int) -> None:
    r = await get_redis()
    await r.set(key, value, ex=ttl)
