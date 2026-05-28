"""
Follow-up pós-aula experimental (D+1) — roda diariamente às 08:00 (SP).

Busca no SQLite os leads com `dia_aula` = ontem (dd/MM/yyyy) e distribui os
envios aleatoriamente na janela 08:00–09:00. Limpa `dia_aula` após envio (ou
após simulação em dry-run) para não reenviar.
"""
import logging
from datetime import date, timedelta

from app import db
from app.config import settings
from app.followups.templates import POST_TRIAL_DAY_AFTER
from app.services import redis_service as rds, uazapi
from app.services.scheduling import distribute_over_window

logger = logging.getLogger("followup.post_trial")

SEND_WINDOW_SECONDS = 3600  # 08:00–09:00
DEDUP_TTL_SECONDS = 26 * 3600  # > 1 ciclo do job para sobrar margem


async def _send_post_trial(lead: dict) -> None:
    phone = lead["phone"]
    text = POST_TRIAL_DAY_AFTER

    if settings.FOLLOWUP_DRY_RUN:
        logger.info("[DRY_RUN][%s] post_trial -> %s", phone, text[:120])
        await db.clear_dia_aula(phone)
        return

    # SETNX atomico: dedup entre clear_dia_aula e proximo ciclo cobre o caso
    # de 2 schedulers rodando concorrentemente (rolling update / overlap).
    flag_key = f"post_trial:sent:{phone}"
    if not await rds.try_set_flag_nx(flag_key, ttl=DEDUP_TTL_SECONDS):
        return

    try:
        await uazapi.send_text(phone, text)
    except Exception as e:
        logger.exception("[%s] falha ao enviar post_trial: %s", phone, e)
        await rds.clear_flag(flag_key)
        return

    await db.clear_dia_aula(phone)


async def run() -> None:
    yesterday = (date.today() - timedelta(days=1)).strftime("%d/%m/%Y")
    due = await db.get_post_trial_due(yesterday)

    if not due:
        return

    logger.info("post_trial: %d aluno(s) com aula em %s", len(due), yesterday)

    await distribute_over_window(
        due, _send_post_trial, window_seconds=SEND_WINDOW_SECONDS, label="post_trial"
    )
