"""
Scheduler: executa os jobs de follow-up (reativação IA, vencimento de plano,
aniversário, pós-aula D+1, ausentes >3d) via APScheduler.

TODOS os jobs rodam apenas de seg a sex (a academia não envia mensagens em
fim de semana). Os diários (plan_expiry, birthday, post_trial, absent)
disparam às 08:00 SP e distribuem os envios em janela aleatória de 1h
(08:00–09:00) via `app.services.scheduling.distribute_over_window`.
`reactivation` roda a cada minuto seg-sex.
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import init_db
from app.followups import absent, birthday, connection_watch, plan_expiry, post_trial, reactivation

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("scheduler")


async def main() -> None:
    await init_db()

    tz = settings.SCHEDULER_TZ
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        reactivation.run,
        CronTrigger(day_of_week="mon-fri", hour="9-18", minute="*/15", timezone=tz),
        id="reactivation",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        plan_expiry.run,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=tz),
        id="plan_expiry",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        birthday.run,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=tz),
        id="birthday",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        post_trial.run,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=tz),
        id="post_trial",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        absent.run,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=tz),
        id="absent",
        max_instances=1,
        coalesce=True,
    )

    # Vigia de conexao: independe de client.yaml e roda 24/7 de proposito — o
    # WhatsApp cai a qualquer hora e a queda so e detectavel por polling.
    if settings.CONNECTION_WATCH_ENABLED:
        watch_min = max(int(settings.CONNECTION_WATCH_MINUTES), 1)
        scheduler.add_job(
            connection_watch.run,
            CronTrigger(minute=f"*/{watch_min}" if watch_min > 1 else "*", timezone=tz),
            id="connection_watch",
            max_instances=1,
            coalesce=True,
        )
        logger.info("job connection_watch: cadencia %d min", watch_min)

    scheduler.start()
    logger.info("Scheduler iniciado (tz=%s). Jobs: %s", tz, [j.id for j in scheduler.get_jobs()])

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
