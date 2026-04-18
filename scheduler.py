"""
Scheduler: executa os jobs de follow-up (reativação IA, vencimento de plano,
aniversário, pós-aula D+1, ausentes >3d) via APScheduler.

Todos os jobs diários (menos o `reactivation`, que já roda a cada minuto)
disparam às 08:00 SP. Cada um distribui os envios internamente em janela
aleatória de 1h (08:00–09:00) via `app.services.scheduling.distribute_over_window`.

`absent` só roda seg-sex — os demais rodam todos os dias.
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import init_db
from app.followups import reactivation, plan_expiry, birthday, post_trial, absent

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
        CronTrigger(minute="*", timezone=tz),
        id="reactivation",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        plan_expiry.run,
        CronTrigger(hour=8, minute=0, timezone=tz),
        id="plan_expiry",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        birthday.run,
        CronTrigger(hour=8, minute=0, timezone=tz),
        id="birthday",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        post_trial.run,
        CronTrigger(hour=8, minute=0, timezone=tz),
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

    scheduler.start()
    logger.info("Scheduler iniciado (tz=%s). Jobs: %s", tz, [j.id for j in scheduler.get_jobs()])

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
