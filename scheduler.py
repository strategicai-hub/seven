"""
Scheduler: executa os 4 jobs de follow-up (reativação IA, vencimento de plano,
aniversário, pós-aula D+1) via APScheduler.
"""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.db import init_db
from app.followups import reactivation, plan_expiry, birthday, post_trial

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
        CronTrigger(hour=9, minute=0, timezone=tz),
        id="plan_expiry",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        birthday.run,
        CronTrigger(hour=9, minute=7, timezone=tz),
        id="birthday",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        post_trial.run,
        CronTrigger(hour=9, minute=4, timezone=tz),
        id="post_trial",
        max_instances=1,
        coalesce=True,
    )

    scheduler.start()
    logger.info("Scheduler iniciado (tz=%s). Jobs: %s", tz, [j.id for j in scheduler.get_jobs()])

    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    asyncio.run(main())
