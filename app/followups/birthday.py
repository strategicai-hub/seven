"""
Aniversariantes — executa diariamente às 09:07 (SP).

Consulta CloudGym v2, filtra quem faz aniversário hoje (dia/mês) e envia a
imagem comemorativa (Wix) via Uazapi send/media.
"""
import logging
import re
from datetime import date

from app.config import settings
from app.images import MEDIA_DICT
from app.services import cloudgym, uazapi
from app.services import redis_service as rds

logger = logging.getLogger("followup.birthday")


def _get_phone(member: dict) -> str:
    raw = str(member.get("cellphonenumber") or member.get("phonenumber") or "")
    return re.sub(r"\D", "", raw)


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

    for m in birthdays:
        phone = _get_phone(m)
        if not phone:
            continue

        flag_key = f"aniv_seven:{phone}"
        if await rds.has_flag(flag_key):
            continue

        if settings.FOLLOWUP_DRY_RUN:
            logger.info("[DRY_RUN][%s] envio de imagem de aniversário", phone)
        else:
            try:
                await uazapi.send_image(phone, image_url)
            except Exception as e:
                logger.exception("[%s] falha ao enviar imagem: %s", phone, e)
                continue

        await rds.set_flag(flag_key, ttl=26 * 3600)
