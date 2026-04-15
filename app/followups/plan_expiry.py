"""
Lembrete de vencimento de plano — executa diariamente às 09:00 (SP).

Consulta CloudGym v2 listando todos os membros, filtra quem vence em 7 ou 15
dias e envia o template fixo correspondente via Uazapi. Usa flag Redis com
TTL 3h para evitar reenvio no mesmo dia.
"""
import asyncio
import logging
import random
import re
from datetime import date

from app.config import settings
from app.services import cloudgym, uazapi
from app.services import redis_service as rds
from app.followups.templates import (
    PLAN_EXPIRY_7D_MENSAL,
    PLAN_EXPIRY_15D_SEMESTRAL,
    PLAN_EXPIRY_FALLBACK,
    primeiro_nome,
)

logger = logging.getLogger("followup.plan_expiry")


def _pick_template(member: dict, days: int) -> str:
    """Escolhe o template baseado no tipo de plano e dias até o vencimento."""
    plan_name = str(member.get("planName") or member.get("plan") or "").lower()
    nome = primeiro_nome(member.get("name"))

    if days == 7 and ("mens" in plan_name or re.search(r"\b1\s*m[êe]s\b", plan_name)):
        return PLAN_EXPIRY_7D_MENSAL.format(primeiroNome=nome)
    if days == 15 and ("sem" in plan_name or "6" in plan_name):
        return PLAN_EXPIRY_15D_SEMESTRAL.format(primeiroNome=nome)
    return PLAN_EXPIRY_FALLBACK.format(primeiroNome=nome)


def _get_phone(member: dict) -> str:
    raw = str(member.get("cellphonenumber") or member.get("phonenumber") or "")
    phone = re.sub(r"\D", "", raw)
    return phone


async def _send_reminder(member: dict, days: int) -> None:
    phone = _get_phone(member)
    if not phone:
        return

    flag_key = f"lembrete_seven:{phone}"
    if await rds.has_flag(flag_key):
        logger.info("[%s] flag ativa, pulando", phone)
        return

    text = _pick_template(member, days)

    if settings.FOLLOWUP_DRY_RUN:
        logger.info("[DRY_RUN][%s] %dd -> %s", phone, days, text[:120])
    else:
        try:
            await uazapi.send_text(phone, text)
        except Exception as e:
            logger.exception("[%s] falha no envio: %s", phone, e)
            return

    await rds.set_flag(flag_key, ttl=3 * 3600)

    # Jitter 10-30min entre envios (evita rate limit / detecção)
    wait = random.randint(600, 1800)
    logger.info("[%s] aguardando %ds antes do próximo envio", phone, wait)
    await asyncio.sleep(wait)


async def run() -> None:
    today = date.today()
    try:
        expiring_7 = await cloudgym.list_members_expiring(7, reference=today)
        expiring_15 = await cloudgym.list_members_expiring(15, reference=today)
    except Exception as e:
        logger.exception("Falha consultando CloudGym: %s", e)
        return

    logger.info("plan_expiry: 7d=%d 15d=%d", len(expiring_7), len(expiring_15))

    for m in expiring_7:
        await _send_reminder(m, 7)
    for m in expiring_15:
        await _send_reminder(m, 15)
