"""Follow-up de alunos ausentes — executa seg-sex às 08:00 (SP).

Fluxo:
  1. Lista todos os membros da unit (CloudGym v2 /v1/member).
  2. Filtra alunos ativos (enddate >= hoje) e remove staff por padrão no nome.
  3. Para cada um, consulta CloudGym v1 /customer/attendance/{id} pegando a
     última presença.
  4. Filtra quem tem última presença há MORE_THAN `DIAS_CORTE` dias (default: 3).
  5. Aplica dedup Redis (`absent:sent:{memberid}`, TTL 7d) — aluno recebe só
     1 mensagem a cada 7 dias mesmo se continuar ausente.
  6. Gera mensagem personalizada via Gemini (sem tools, prompt enxuto).
  7. Distribui os envios na janela 0-3600s (entre 08:00 e 09:00).

Respeita FOLLOWUP_DRY_RUN — em dry-run, loga o que seria enviado e NÃO marca
a flag Redis (pra permitir rodadas de teste repetidas).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime
from typing import Optional

from google.genai import types as gtypes

from app.config import settings
from app.followups.templates import primeiro_nome
from app.services import cloudgym, uazapi
from app.services import redis_service as rds
from app.services.gemini import _get_client, call_with_retry
from app.services.scheduling import distribute_over_window

logger = logging.getLogger("followup.absent")

DIAS_CORTE = 3
DEDUP_TTL_SECONDS = 7 * 24 * 3600
SEND_WINDOW_SECONDS = 3600  # 08:00–09:00
ATTENDANCE_CONCURRENCY = 2
# CloudGym v1 rate-limita agressivamente (/customer/attendance). Mesmo com
# concorrência baixa, rajadas provocam 429 — esse delay dentro do semáforo
# garante ~6 req/s no pior caso.
ATTENDANCE_MIN_INTERVAL_SECONDS = 0.3

# Marcadores em (parenteses) que identificam funcionários da academia.
# Convênios (CRESOL, FATEC, OAB, SEED, ALIANÇA, BOMBEIRO, PROSPORT etc.) NÃO
# são staff — quem tem esses marcadores é aluno comum e DEVE receber follow-up.
_STAFF_TOKENS = [
    "RECEPCIONISTA",
    "INSTRUTOR",  # cobre INSTRUTOR e INSTRUTORA
    "PROFESSOR",  # cobre PROFESSOR/A
    "PROF.",
    "PROF ",  # PROF THAI, PROF FITDANCE
    "PERSONAL",
    "GESTOR",
    "GERENCIAL",
    "MARKETING",
    "FISIOTERAPEUTA",
    "ZELADORA",
    "ATLETA JIU JITSU",
]


def _is_staff(name: str | None) -> bool:
    up = (name or "").upper()
    # olha só dentro do trecho entre parênteses
    m = re.search(r"\(([^()]+)\)", up)
    if not m:
        return False
    tag = m.group(1).strip()
    return any(tok in tag for tok in _STAFF_TOKENS)


def _get_phone(member: dict) -> str:
    raw = str(member.get("cellphonenumber") or member.get("cellPhoneNumber")
              or member.get("phonenumber") or member.get("phone") or "")
    return re.sub(r"\D", "", raw)


def _parse_iso_date(value: str | None) -> Optional[date]:
    if not value:
        return None
    head = value.split("T", 1)[0].split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    return None


def _is_active(m: dict, today: date) -> bool:
    end = _parse_iso_date(str(m.get("enddate") or m.get("endDate") or ""))
    if end is None:
        return True
    return end >= today


async def _last_attendance_date(member: dict, sem: asyncio.Semaphore) -> Optional[date]:
    mid = member.get("memberid") or member.get("memberId") or member.get("id")
    if not mid:
        return None
    async with sem:
        await asyncio.sleep(ATTENDANCE_MIN_INTERVAL_SECONDS)
        try:
            items = await cloudgym.get_member_attendance(mid, size=1, sort="date,desc")
        except Exception as e:
            logger.warning("memberid=%s erro ao buscar attendance: %s", mid, e)
            return None
    if not items:
        return None
    raw = items[0].get("date") or items[0].get("attendanceDate") or items[0].get("createdAt") or ""
    return _parse_iso_date(str(raw))


# ---------------- geração de mensagem via Gemini ----------------

_ABSENT_PROMPT_TEMPLATE = """Você é a Zoe, atendente jovem e simpática da Academia Seven Fitness (Seven Academia).
Gere UMA mensagem curta em português brasileiro para enviar via WhatsApp a um aluno matriculado que não aparece na academia há alguns dias.

Regras:
- Tom acolhedor, sem pressão nem cobrança.
- Use o primeiro nome do aluno.
- No máximo 3 linhas.
- Sem emojis.
- Sem markdown, sem aspas, sem prefixos — retorne apenas o texto final da mensagem.
- Termine com uma pergunta aberta e leve, convidando a retomar o treino.
- NÃO mencione promoções, desconto, nem ofertas.
- NÃO mencione o número exato de dias — seja natural ("há alguns dias", "essa semana", etc.).
- Varie o estilo a cada execução pra não parecer robotizada.

Dados:
- Nome completo: {nome}
- Dias sem frequentar: {dias}
"""


async def _generate_message(nome: str, dias: int) -> str:
    client = _get_client()
    prompt = _ABSENT_PROMPT_TEMPLATE.format(nome=nome, dias=dias)

    def _call():
        return client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=[gtypes.Content(role="user", parts=[gtypes.Part.from_text(text=prompt)])],
        )

    try:
        resp = await call_with_retry(_call, label="gemini.absent", max_tries=3)
        text = (getattr(resp, "text", None) or "").strip()
        return text
    except Exception as e:
        logger.exception("Falha no Gemini gerando msg absent: %s", e)
        return ""


def _fallback_message(nome: str) -> str:
    primeiro = primeiro_nome(nome)
    return (
        f"Oi {primeiro}, aqui é a Zoe da Seven. "
        "Senti sua falta aqui na academia essa semana — tá tudo bem? "
        "O que acha de marcar um horário pra voltar pro treino?"
    )


# ---------------- envio ----------------

async def _send_one(member_info: tuple[dict, int]) -> None:
    member, dias = member_info
    phone = _get_phone(member)
    nome = str(member.get("name") or "")
    mid = member.get("memberid") or member.get("memberId") or member.get("id")

    if not phone:
        logger.info("memberid=%s sem telefone, pulando", mid)
        return

    flag_key = f"absent:sent:{mid}"
    if await rds.has_flag(flag_key):
        logger.info("[%s] memberid=%s dedup ativo, pulando", phone, mid)
        return

    text = await _generate_message(nome, dias)
    if not text:
        text = _fallback_message(nome)
        logger.warning("[%s] fallback de mensagem (Gemini falhou)", phone)

    if settings.FOLLOWUP_DRY_RUN:
        logger.info("[DRY_RUN][%s] memberid=%s dias=%d msg=%r", phone, mid, dias, text[:160])
        return  # NÃO marca flag em dry-run — permite múltiplos testes

    try:
        await uazapi.send_text(phone, text)
    except Exception as e:
        logger.exception("[%s] falha ao enviar absent: %s", phone, e)
        return

    await rds.set_flag(flag_key, ttl=DEDUP_TTL_SECONDS)


# ---------------- job principal ----------------

async def collect_targets(
    today: Optional[date] = None, limit: Optional[int] = None
) -> list[tuple[dict, int]]:
    """Retorna lista de (member, dias_ausente) elegíveis para envio hoje.

    Não chama Uazapi nem Gemini — pode ser usada em dry-run/inspeção.
    `limit` trunca a lista de membros ATIVOS antes das chamadas de attendance
    (útil para testes rápidos).
    """
    today = today or date.today()
    try:
        members = await cloudgym.list_all_members()
    except Exception:
        logger.exception("Falha listando membros da CloudGym")
        return []

    ativos = [m for m in members if _is_active(m, today) and not _is_staff(m.get("name"))]
    logger.info(
        "absent: %d total, %d após filtro ativo+não-staff",
        len(members), len(ativos),
    )
    if limit:
        ativos = ativos[:limit]
        logger.info("absent: truncando para os primeiros %d (limit)", limit)

    sem = asyncio.Semaphore(ATTENDANCE_CONCURRENCY)
    tasks = [_last_attendance_date(m, sem) for m in ativos]
    dates = await asyncio.gather(*tasks, return_exceptions=False)

    targets: list[tuple[dict, int]] = []
    for m, last in zip(ativos, dates):
        if last is None:
            # nunca frequentou — inclui com "dias" alto
            targets.append((m, 999))
            continue
        dias = (today - last).days
        if dias > DIAS_CORTE:
            targets.append((m, dias))

    logger.info("absent: %d aluno(s) ausentes há mais de %dd", len(targets), DIAS_CORTE)
    return targets


async def run() -> None:
    targets = await collect_targets()
    if not targets:
        return

    await distribute_over_window(
        targets,
        _send_one,
        window_seconds=SEND_WINDOW_SECONDS,
        label="absent",
    )
