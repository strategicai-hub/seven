import json
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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


def _block_ttl_seconds() -> int:
    # Bloqueio expira amanhã às 08:00 SP — Zoe só volta no dia seguinte.
    tz = ZoneInfo("America/Sao_Paulo")
    now = datetime.now(tz)
    target = (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    return max(int((target - now).total_seconds()), 60)


# ---- bloqueio de agente ----

async def set_block(phone: str, ttl: int | None = None) -> None:
    r = await get_redis()
    await r.set(f"{_base_key(phone)}:block", "1", ex=ttl or _block_ttl_seconds())


async def is_blocked(phone: str) -> bool:
    r = await get_redis()
    return await r.exists(f"{_base_key(phone)}:block") == 1




# ---- ecos de mensagens enviadas pela propria API ----

def _outbound_digest(text: str) -> str:
    normalized = (text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _outbound_echo_key(phone: str, digest: str) -> str:
    return f"{_base_key(phone)}:outbound:{digest}"


async def mark_outbound_echo(phone: str, text: str, ttl: int = 120) -> None:
    if not phone or not text:
        return
    r = await get_redis()
    await r.set(_outbound_echo_key(phone, _outbound_digest(text)), "1", ex=ttl)


async def consume_outbound_echo(phone: str, text: str) -> bool:
    if not phone or not text:
        return False
    r = await get_redis()
    deleted = await r.delete(_outbound_echo_key(phone, _outbound_digest(text)))
    return deleted == 1

# ---- buffer de mensagens (debounce) ----

def _buffer_key(phone: str) -> str:
    return f"{_base_key(phone)}:buffer"


async def push_buffer(phone: str, text: str) -> int:
    r = await get_redis()
    return await r.rpush(_buffer_key(phone), text)


async def prepend_buffer(phone: str, text: str) -> int:
    """Insere no início do buffer. Usado ao abortar uma resposta obsoleta para
    que o próximo ciclo processe a msg antiga + as novas que chegaram juntas."""
    r = await get_redis()
    return await r.lpush(_buffer_key(phone), text)


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
    await r.ltrim(_history_key(phone), -10, -1)


async def clear_chat_history(phone: str) -> None:
    r = await get_redis()
    await r.delete(_history_key(phone))


async def reset_lead_state(phone: str) -> None:
    """Apaga TODAS as chaves Redis relacionadas ao lead — usado pelo /reset.
    Inclui: histórico, buffer, bloqueio humano, flag de alerta, e dedups
    de follow-up (aniversário e plano expirando)."""
    r = await get_redis()
    base = _base_key(phone)
    await r.delete(
        _history_key(phone),
        _buffer_key(phone),
        f"{base}:block",
        f"{base}:alert",
        f"aniv_seven:{phone}",
        f"lembrete_seven:{phone}",
    )


async def pop_last_history(phone: str, n: int = 1) -> None:
    """Remove as últimas `n` entradas do histórico. Usado no abort para
    descartar user+model registrados numa chamada Gemini cuja resposta
    não chegou a ser enviada ao lead."""
    r = await get_redis()
    for _ in range(n):
        await r.rpop(_history_key(phone))


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
