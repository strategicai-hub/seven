"""
Camada SQLite (aiosqlite) para estado dos leads e follow-ups.
Tabela `leads` é fonte da verdade para: dia_aula (pós-aula D+1), next_follow_up
(reativação), status_conversa (finalizado), modo_mudo (após atendimento humano).
"""
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  phone TEXT PRIMARY KEY,
  nome TEXT,
  status TEXT,
  modo_mudo INTEGER DEFAULT 0,
  dia_aula TEXT,
  next_follow_up TEXT,
  stage_follow_up INTEGER DEFAULT 0,
  status_conversa TEXT DEFAULT 'novo',
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_next_fup ON leads(next_follow_up, status_conversa);
CREATE INDEX IF NOT EXISTS idx_leads_dia_aula ON leads(dia_aula);
"""


def _ensure_dir() -> None:
    path = settings.SQLITE_PATH
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)


def init_db_sync() -> None:
    """Versão síncrona para o startup do FastAPI."""
    _ensure_dir()
    con = sqlite3.connect(settings.SQLITE_PATH)
    try:
        con.executescript(SCHEMA)
        con.commit()
        logger.info("SQLite inicializado em %s", settings.SQLITE_PATH)
    finally:
        con.close()


async def init_db() -> None:
    _ensure_dir()
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    logger.info("SQLite inicializado em %s", settings.SQLITE_PATH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


async def upsert_lead(phone: str, **fields) -> None:
    """Cria ou atualiza um lead. Campos ausentes ficam como estão."""
    if not phone:
        return
    fields["updated_at"] = _now_iso()
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        cur = await db.execute("SELECT phone FROM leads WHERE phone=?", (phone,))
        row = await cur.fetchone()
        if row is None:
            cols = ["phone"] + list(fields.keys())
            vals = [phone] + list(fields.values())
            placeholders = ",".join(["?"] * len(cols))
            await db.execute(
                f"INSERT INTO leads ({','.join(cols)}) VALUES ({placeholders})",
                vals,
            )
        else:
            if fields:
                assigns = ",".join(f"{k}=?" for k in fields.keys())
                await db.execute(
                    f"UPDATE leads SET {assigns} WHERE phone=?",
                    list(fields.values()) + [phone],
                )
        await db.commit()


async def get_lead(phone: str) -> Optional[dict]:
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM leads WHERE phone=?", (phone,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_modo_mudo(phone: str, value: bool = True) -> None:
    await upsert_lead(phone, modo_mudo=1 if value else 0)


async def is_modo_mudo(phone: str) -> bool:
    lead = await get_lead(phone)
    return bool(lead and lead.get("modo_mudo"))


async def set_dia_aula(phone: str, dia_aula_str: str) -> None:
    """dia_aula_str no formato dd/MM/yyyy."""
    await upsert_lead(phone, dia_aula=dia_aula_str)


async def schedule_followup(phone: str, next_follow_up_iso: str, stage: int = 1) -> None:
    await upsert_lead(
        phone,
        next_follow_up=next_follow_up_iso,
        stage_follow_up=stage,
        status_conversa="em_andamento",
    )


async def get_followups_due(now_iso: str) -> list[dict]:
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM leads
            WHERE next_follow_up IS NOT NULL
              AND next_follow_up <= ?
              AND COALESCE(status_conversa, '') != 'finalizado'
              AND COALESCE(modo_mudo, 0) = 0
            """,
            (now_iso,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_post_trial_due(dia_aula_str: str) -> list[dict]:
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT * FROM leads
            WHERE dia_aula = ?
              AND COALESCE(modo_mudo, 0) = 0
            """,
            (dia_aula_str,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def advance_followup_stage(phone: str, new_stage: int, next_iso: Optional[str], finalize: bool) -> None:
    fields = {
        "stage_follow_up": new_stage,
        "next_follow_up": next_iso,
    }
    if finalize:
        fields["status_conversa"] = "finalizado"
        fields["next_follow_up"] = None
    await upsert_lead(phone, **fields)


async def clear_dia_aula(phone: str) -> None:
    await upsert_lead(phone, dia_aula=None)


async def mark_finalizado(phone: str) -> None:
    await upsert_lead(phone, status_conversa="finalizado", next_follow_up=None)


async def list_all_leads() -> list[dict]:
    async with aiosqlite.connect(settings.SQLITE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM leads ORDER BY updated_at DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
