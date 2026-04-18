"""
Lembrete de vencimento de plano — roda diariamente às 08:00 (SP).

Lista todos os membros na CloudGym v2, filtra quem vence em 7 ou 15 dias e
distribui os envios aleatoriamente na janela 08:00–09:00. Usa flag Redis
com TTL 3h para evitar reenvio no mesmo dia.
"""
import logging
import re
from datetime import date

from app.config import settings
from app.followups.templates import (
    PLAN_EXPIRY_7D_MENSAL,
    PLAN_EXPIRY_15D_SEMESTRAL,
    PLAN_EXPIRY_FALLBACK,
    primeiro_nome,
)
from app.services import cloudgym, uazapi
from app.services import redis_service as rds
from app.services.scheduling import distribute_over_window

logger = logging.getLogger("followup.plan_expiry")

SEND_WINDOW_SECONDS = 3600  # 08:00–09:00


def _pick_template(member: dict, days: int) -> str:
    plan_name = str(member.get("planName") or member.get("plan") or "").lower()
    nome = primeiro_nome(member.get("name"))

    if days == 7 and ("mens" in plan_name or re.search(r"\b1\s*m[êe]s\b", plan_name)):
        return PLAN_EXPIRY_7D_MENSAL.format(primeiroNome=nome)
    if days == 15 and ("sem" in plan_name or "6" in plan_name):
        return PLAN_EXPIRY_15D_SEMESTRAL.format(primeiroNome=nome)
    return PLAN_EXPIRY_FALLBACK.format(primeiroNome=nome)


def _get_phone(member: dict) -> str:
    raw = str(member.get("cellphonenumber") or member.get("phonenumber") or "")
    return re.sub(r"\D", "", raw)


async def _send_reminder(item: tuple[dict, int]) -> None:
    member, days = item
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
        return

    try:
        await uazapi.send_text(phone, text)
    except Exception as e:
        logger.exception("[%s] falha no envio: %s", phone, e)
        return

    await rds.set_flag(flag_key, ttl=3 * 3600)


async def run() -> None:
    today = date.today()
    try:
        expiring_7 = await cloudgym.list_members_expiring(7, reference=today)
        expiring_15 = await cloudgym.list_members_expiring(15, reference=today)
    except Exception as e:
        logger.exception("Falha consultando CloudGym: %s", e)
        return

    logger.info("plan_expiry: 7d=%d 15d=%d", len(expiring_7), len(expiring_15))

    items: list[tuple[dict, int]] = [(m, 7) for m in expiring_7] + [(m, 15) for m in expiring_15]
    await distribute_over_window(
        items, _send_reminder, window_seconds=SEND_WINDOW_SECONDS, label="plan_expiry"
    )
