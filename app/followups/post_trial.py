"""
Follow-up pós-aula experimental (D+1) — executa diariamente às 09:04 (SP).

Busca no SQLite os leads com `dia_aula` = ontem (dd/MM/yyyy), envia o template
fixo via Uazapi e limpa `dia_aula` para não reenviar.
"""
import logging
from datetime import date, timedelta

from app import db
from app.config import settings
from app.followups.templates import POST_TRIAL_DAY_AFTER
from app.services import uazapi

logger = logging.getLogger("followup.post_trial")


async def run() -> None:
    yesterday = (date.today() - timedelta(days=1)).strftime("%d/%m/%Y")
    due = await db.get_post_trial_due(yesterday)

    if not due:
        return

    logger.info("post_trial: %d aluno(s) com aula em %s", len(due), yesterday)

    for lead in due:
        phone = lead["phone"]
        text = POST_TRIAL_DAY_AFTER

        if settings.FOLLOWUP_DRY_RUN:
            logger.info("[DRY_RUN][%s] post_trial -> %s", phone, text[:120])
        else:
            try:
                await uazapi.send_text(phone, text)
            except Exception as e:
                logger.exception("[%s] falha ao enviar post_trial: %s", phone, e)
                continue

        await db.clear_dia_aula(phone)
