"""
Aniversariantes — roda diariamente às 08:00 (SP).

Lista aniversariantes de hoje (dia/mês) na CloudGym v2 e distribui os envios
aleatoriamente na janela 08:00–09:00. Usa flag Redis (TTL 26h) para evitar
reenvio dentro do mesmo ciclo.
"""
import logging
import re
from datetime import date

from app.config import settings
from app.images import MEDIA_DICT
from app.services import cloudgym, uazapi
from app.services import redis_service as rds
from app.services.scheduling import distribute_over_window

logger = logging.getLogger("followup.birthday")

SEND_WINDOW_SECONDS = 3600  # 08:00–09:00


def _get_phone(member: dict) -> str:
    raw = str(member.get("cellphonenumber") or member.get("phonenumber") or "")
    return re.sub(r"\D", "", raw)


async def _send_birthday(ctx: tuple[dict, str]) -> None:
    member, image_url = ctx
    phone = _get_phone(member)
    if not phone:
        return

    flag_key = f"aniv_seven:{phone}"
    if await rds.has_flag(flag_key):
        return

    if settings.FOLLOWUP_DRY_RUN:
        logger.info("[DRY_RUN][%s] envio de imagem de aniversário", phone)
        return

    try:
        await uazapi.send_image(phone, image_url)
    except Exception as e:
        logger.exception("[%s] falha ao enviar imagem: %s", phone, e)
        return

    await rds.set_flag(flag_key, ttl=26 * 3600)


async def run() -> None:
    today = date.today()
    try:
        birthdays = await cloudgym.list_members_birthday(reference=today)
    except Exception as e:
        logger.exception("Falha consultando CloudGym: %s", e)
        return

    logger.info("birthday: %d aniversariante(s)", len(birthdays))

    image_url = MEDIA_DICT.get("[IMAGEM_ANIVERSARIO]", {}).get("url")
    if not image_url:
        logger.error("URL de imagem de aniversário não configurada")
        return

    items = [(m, image_url) for m in birthdays]
    await distribute_over_window(
        items, _send_birthday, window_seconds=SEND_WINDOW_SECONDS, label="birthday"
    )
