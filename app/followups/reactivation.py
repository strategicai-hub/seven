"""
Reativação de leads — executa a cada 1 minuto.

Busca leads com next_follow_up <= now e status_conversa != 'finalizado'. Para
cada um: gera mensagem personalizada via Gemini (prompt de reativação), envia
via Uazapi, avança estágio e reagenda próximo dia (ou finaliza no estágio 3).
"""
import logging
from datetime import datetime, timedelta, timezone

from zoneinfo import ZoneInfo

from app import db
from app.config import settings
from app.services import uazapi
from app.services.gemini import generate_reactivation_message

logger = logging.getLogger("followup.reactivation")


def _now_tz() -> datetime:
    return datetime.now(ZoneInfo(settings.SCHEDULER_TZ))


async def run() -> None:
    now_tz = _now_tz()
    now_utc = now_tz.astimezone(timezone.utc).isoformat()

    due = await db.get_followups_due(now_utc)
    if not due:
        return

    logger.info("reactivation: %d lead(s) devido(s)", len(due))

    for lead in due:
        phone = lead["phone"]
        nome = lead.get("nome") or ""
        stage = int(lead.get("stage_follow_up") or 1)

        if stage > 3:
            await db.mark_finalizado(phone)
            continue

        now_str = now_tz.strftime("%A, %d/%m/%Y %H:%M")
        try:
            msg = await generate_reactivation_message(phone, nome, stage, now_str)
        except Exception as e:
            logger.warning("[%s] falha no Gemini: %s", phone, e)
            continue

        if not msg:
            logger.info("[%s] mensagem vazia (stage=%d), pulando", phone, stage)
            continue

        if settings.FOLLOWUP_DRY_RUN:
            logger.info("[DRY_RUN][%s] stage=%d -> %s", phone, stage, msg[:160])
        else:
            try:
                await uazapi.send_text(phone, msg)
            except Exception as e:
                logger.exception("[%s] falha ao enviar reativação: %s", phone, e)
                continue

        # Avança estágio
        finalize = stage >= 3
        new_stage = stage + 1 if not finalize else 3
        next_iso = None
        if not finalize:
            next_iso = (now_tz + timedelta(days=1)).astimezone(timezone.utc).isoformat()

        await db.advance_followup_stage(phone, new_stage, next_iso, finalize)
        logger.info("[%s] stage %d -> %d (finalize=%s)", phone, stage, new_stage, finalize)
