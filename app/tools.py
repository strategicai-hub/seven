"""
Tools expostas ao Gemini via function calling.

Cada tool tem:
- uma FunctionDeclaration (schema que vai ao modelo)
- um handler assíncrono (executado localmente quando o modelo chama a função)

O handler retorna um dict que será entregue ao modelo como `function_response`.
O modelo então decide se chama outra tool ou se gera texto final.
"""
import asyncio
import logging
import unicodedata
from datetime import datetime, timedelta
from typing import Any

from google.genai import types as gtypes

from app import db
from app.config import settings
from app.data import class_catalog as catalog
from app.services import cloudgym, uazapi

logger = logging.getLogger(__name__)


# ---------------- helpers ----------------

def _normalize(s: str) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def _parse_time(val: str) -> tuple[int, int] | None:
    """Aceita 'HH:mm', 'HH:mm:ss', 'HHmm'. Retorna (h, m) ou None."""
    if not val:
        return None
    s = str(val).strip()
    try:
        if ":" in s:
            h, m = s.split(":")[:2]
            return int(h), int(m)
        if len(s) == 4 and s.isdigit():
            return int(s[:2]), int(s[2:])
    except Exception:
        return None
    return None


# ---------------- FunctionDeclarations ----------------

SCHEMA_SALVA_NOME = gtypes.FunctionDeclaration(
    name="salva_nome",
    description="Salva o primeiro nome do lead no CRM. Chame assim que o lead informar o nome.",
    parameters=gtypes.Schema(
        type=gtypes.Type.OBJECT,
        properties={
            "nome": gtypes.Schema(type=gtypes.Type.STRING, description="Primeiro nome do lead"),
        },
        required=["nome"],
    ),
)

SCHEMA_CLASSIFICA = gtypes.FunctionDeclaration(
    name="classifica_contato",
    description=(
        "Classifica o contato como 'lead' (novo interessado) ou 'aluno' (já matriculado). "
        "Chame SEMPRE na primeira troca. Aluno = dúvidas de pagamento, renovação, PIX, boleto, app."
    ),
    parameters=gtypes.Schema(
        type=gtypes.Type.OBJECT,
        properties={
            "tipo": gtypes.Schema(type=gtypes.Type.STRING, description="'lead' ou 'aluno'"),
        },
        required=["tipo"],
    ),
)

SCHEMA_LISTA_HORARIOS = gtypes.FunctionDeclaration(
    name="lista_horarios",
    description=(
        "Consulta a disponibilidade REAL de aulas experimentais no sistema da academia "
        "(CloudGym) para uma modalidade e data. Retorna até 3 horários futuros (descarta "
        "aulas que começam em menos de 30 minutos). Chame antes de propor horários."
    ),
    parameters=gtypes.Schema(
        type=gtypes.Type.OBJECT,
        properties={
            "modalidade": gtypes.Schema(
                type=gtypes.Type.STRING,
                description="Ex: 'seven cross', 'muay thai', 'fitdance', 'seven bike', 'seven pump', 'bike move', 'muay thai kids'",
            ),
            "data": gtypes.Schema(
                type=gtypes.Type.STRING,
                description="Data no formato yyyy-MM-dd. Nunca domingo.",
            ),
        },
        required=["modalidade", "data"],
    ),
)

SCHEMA_CATALOGO = gtypes.FunctionDeclaration(
    name="catalogo_horarios",
    description=(
        "Retorna a GRADE FIXA estática de horários por modalidade (fallback). "
        "Use APENAS se lista_horarios falhar, se o lead pedir a grade completa sem data, "
        "ou se precisar saber em que dia da semana a modalidade roda."
    ),
    parameters=gtypes.Schema(
        type=gtypes.Type.OBJECT,
        properties={
            "modalidade": gtypes.Schema(
                type=gtypes.Type.STRING,
                description="Modalidade específica. Vazio retorna todas.",
            ),
        },
    ),
)

SCHEMA_AGENDA_AULA = gtypes.FunctionDeclaration(
    name="agenda_aula",
    description=(
        "Efetiva o agendamento de aula experimental no CloudGym. "
        "Passe a lista 'class_ids' exatamente como veio do slot em lista_horarios "
        "(vários IDs podem corresponder ao mesmo horário em dias diferentes da semana — "
        "a tool tenta cada um até um ser aceito). "
        "Só chame depois do lead confirmar horário e fornecer nome completo (se for novo)."
    ),
    parameters=gtypes.Schema(
        type=gtypes.Type.OBJECT,
        properties={
            "class_ids": gtypes.Schema(
                type=gtypes.Type.ARRAY,
                items=gtypes.Schema(type=gtypes.Type.INTEGER),
                description="Lista EXATA de class_ids numéricos retornados por lista_horarios para o slot escolhido. COPIE os números inteiros exatamente como vieram. PROIBIDO inventar IDs.",
            ),
            "data": gtypes.Schema(type=gtypes.Type.STRING, description="yyyy-MM-dd"),
            "hora": gtypes.Schema(type=gtypes.Type.STRING, description="HH:mm (informativo para log)"),
            "modalidade": gtypes.Schema(
                type=gtypes.Type.STRING,
                description="Nome da modalidade (informativo).",
            ),
            "nome_completo": gtypes.Schema(
                type=gtypes.Type.STRING,
                description="Nome completo do lead — obrigatório se ele ainda não estiver cadastrado.",
            ),
        },
        required=["class_ids", "data"],
    ),
)

SCHEMA_ATENDIMENTO_HUMANO = gtypes.FunctionDeclaration(
    name="atendimento_humano",
    description=(
        "Transfere a conversa para um atendente humano. "
        "Use quando: pedido de matrícula direta, dúvidas financeiras complexas, "
        "cancelamentos, comprovantes de pagamento, ou quando você não conseguir resolver."
    ),
    parameters=gtypes.Schema(
        type=gtypes.Type.OBJECT,
        properties={
            "motivo": gtypes.Schema(
                type=gtypes.Type.STRING,
                description="Motivo curto (<=80 chars) que será enviado à recepção.",
            ),
        },
        required=["motivo"],
    ),
)

ALL_TOOLS = gtypes.Tool(
    function_declarations=[
        SCHEMA_SALVA_NOME,
        SCHEMA_CLASSIFICA,
        SCHEMA_LISTA_HORARIOS,
        SCHEMA_CATALOGO,
        SCHEMA_AGENDA_AULA,
        SCHEMA_ATENDIMENTO_HUMANO,
    ]
)


# ---------------- handlers ----------------

async def handle_salva_nome(phone: str, args: dict) -> dict:
    nome = (args.get("nome") or "").strip()
    if not nome:
        return {"ok": False, "error": "nome vazio"}
    await db.upsert_lead(phone, nome=nome)
    return {"ok": True, "nome": nome}


async def handle_classifica_contato(phone: str, args: dict) -> dict:
    tipo = (args.get("tipo") or "").strip().lower()
    if tipo not in ("lead", "aluno"):
        return {"ok": False, "error": "tipo deve ser 'lead' ou 'aluno'"}
    await db.upsert_lead(phone, status=tipo)
    return {"ok": True, "tipo": tipo}


# Grade fixa usada pelo fallback `catalogo_horarios`.
# Formato compacto: "Seg/Qua 08:00, 15:00, 19:00 | Ter/Qui 06:00"
_GRADE_FIXA: dict[str, str] = {
    "seven_cross": "Seg/Qua/Sex 06:00, 16:15, 18:30",
    "muay_thai": "Seg/Qua 08:00, 15:00, 19:00 | Ter/Qui 06:00, 17:15, 18:15",
    "muay_thai_kids": "Seg/Qua 18:00",
    "fitdance": "Seg/Qua 20:00 | Ter/Qui 17:00",
    "bike_move": "Ter/Qui 19:30 | Sex 18:30",
    "seven_bike": "Seg/Qua 07:00, 18:30 | Ter/Qui 06:00, 08:15, 17:15 | Sex 07:00",
    "seven_pump": "Seg/Qua/Sex 08:15, 17:15 | Ter/Qui 07:00, 18:30",
}


async def handle_catalogo_horarios(phone: str, args: dict) -> dict:
    mod_raw = (args.get("modalidade") or "").strip()
    if mod_raw:
        canon = catalog.resolve_modality(mod_raw)
        if canon and canon in _GRADE_FIXA:
            return {
                "ok": True,
                "modalidade": catalog.DISPLAY_NAME.get(canon, canon),
                "grade": _GRADE_FIXA[canon],
            }
        return {"ok": False, "error": f"modalidade '{mod_raw}' não encontrada"}
    # Sem filtro → retorna tudo (compacto)
    return {
        "ok": True,
        "grade": {catalog.DISPLAY_NAME.get(k, k): v for k, v in _GRADE_FIXA.items()},
    }


def _hhmm(t: str | None) -> str:
    if not t:
        return ""
    return str(t)[:5]  # "06:00:00" -> "06:00"


async def _count_reservations(data_str: str, class_id: int) -> int:
    """Retorna len(attendancelist) para (class_id, data). 0 se vazio/erro."""
    try:
        r = await cloudgym.get_class_availability(data_str, str(class_id))
    except Exception:
        return 0
    items = r if isinstance(r, list) else (r.get("items") or [])
    return len(items)


async def handle_lista_horarios(phone: str, args: dict) -> dict:
    """Consulta /config/classes (cached), agrupa por horário e retorna até 3 slots.

    Fluxo:
    1. Valida weekday contra WEEKDAYS_BY_MODALITY (fail fast, sem bater na API).
    2. Carrega catálogo (1 chamada HTTP, cache 1h).
    3. Filtra por nome da modalidade (campo `name`).
    4. Agrupa por `time` — cada slot pode ter múltiplos class_ids (um por dia da semana).
    5. Filtra slots em passado ou <30min no futuro.
    6. Retorna até 3 slots com {hora, class_ids, capacity, vagas}.
    """
    modalidade = (args.get("modalidade") or "").strip()
    data_str = (args.get("data") or "").strip()
    if not modalidade or not data_str:
        return {"ok": False, "error": "modalidade e data são obrigatórias"}

    try:
        dt = datetime.strptime(data_str, "%Y-%m-%d").date()
    except ValueError:
        return {"ok": False, "error": "data inválida — use yyyy-MM-dd"}

    if dt.weekday() == 6:
        return {"ok": False, "error": "academia não agenda aula experimental aos domingos"}

    canon = catalog.resolve_modality(modalidade)
    if not canon:
        return {"ok": False, "error": f"modalidade '{modalidade}' não encontrada"}

    wd_allowed = catalog.WEEKDAYS_BY_MODALITY.get(canon, set())
    if wd_allowed and dt.weekday() not in wd_allowed:
        return {
            "ok": False,
            "error": "dia_sem_aula",
            "modalidade": catalog.DISPLAY_NAME.get(canon, canon),
            "mensagem_tecnica": f"{catalog.DISPLAY_NAME.get(canon, canon)} não roda no dia {data_str}.",
        }

    # 1 chamada HTTP cacheada
    try:
        classes = await cloudgym.list_classes()
    except Exception as e:
        logger.exception("Falha ao listar classes")
        return {"ok": False, "error": f"falha_catalogo: {e}"}

    alvo_nome = catalog.API_NAME.get(canon, "").upper()
    if not alvo_nome:
        return {"ok": False, "error": f"sem mapeamento de nome para '{canon}'"}

    # Filtra classes da modalidade
    aulas = [c for c in classes if (c.get("name") or "").strip().upper() == alvo_nome]
    if not aulas:
        return {"ok": False, "error": f"nenhuma aula de '{modalidade}' no catálogo"}

    # Agrupa por `time`
    by_time: dict[str, dict] = {}
    for c in aulas:
        hora = _hhmm(c.get("time"))
        cap = int(c.get("capacity") or 0)
        entry = by_time.setdefault(hora, {"hora": hora, "class_ids": [], "capacity": cap})
        entry["class_ids"].append(int(c["id"]))
        # capacity pode variar entre IDs do mesmo horário — pega o maior
        entry["capacity"] = max(entry["capacity"], cap)

    # Filtro de segurança: descarta aulas em passado ou <30min à frente
    now = datetime.now()
    cutoff = now + timedelta(minutes=30)
    slots = []
    for hora, info in by_time.items():
        hm = _parse_time(hora)
        if not hm:
            continue
        slot_dt = datetime.combine(dt, datetime.min.time()).replace(hour=hm[0], minute=hm[1])
        if slot_dt < cutoff:
            continue
        slots.append(info)

    # Ordena por hora e limita a 3
    slots.sort(key=lambda s: _parse_time(s["hora"]) or (99, 99))
    slots = slots[:3]

    # Consulta reservas em paralelo (usa só o primeiro class_id de cada grupo — economiza requests)
    reservas = await asyncio.gather(
        *[_count_reservations(data_str, s["class_ids"][0]) for s in slots],
        return_exceptions=True,
    )
    for s, r in zip(slots, reservas):
        cap = s.get("capacity", 0)
        reservados = r if isinstance(r, int) else 0
        s["vagas"] = max(0, cap - reservados) if cap else None

    return {
        "ok": True,
        "modalidade": catalog.DISPLAY_NAME.get(canon, canon),
        "data": data_str,
        "slots": slots,
    }


def _extract_memberid(member: dict) -> str | None:
    for k in ("memberid", "memberId", "id", "memberID"):
        v = member.get(k)
        if v:
            return str(v)
    return None


def _extract_plan(member: dict) -> str | None:
    for k in ("plan", "planId", "planid", "plan_id"):
        v = member.get(k)
        if v is not None:
            return str(v)
    return None


async def handle_agenda_aula(phone: str, args: dict) -> dict:
    raw_ids = args.get("class_ids") or args.get("class_id")
    if raw_ids is None:
        return {"ok": False, "error": "class_ids é obrigatório"}
    if isinstance(raw_ids, (str, int)):
        raw_ids = [raw_ids]
    class_ids: list[int] = []
    for v in raw_ids:
        try:
            class_ids.append(int(str(v).strip()))
        except (ValueError, TypeError):
            continue
    if not class_ids:
        return {"ok": False, "error": "class_ids inválidos"}

    data_str = (args.get("data") or "").strip()
    hora = (args.get("hora") or "").strip()
    modalidade = (args.get("modalidade") or "").strip()
    nome_completo = (args.get("nome_completo") or "").strip()

    if not data_str:
        return {"ok": False, "error": "data é obrigatória"}

    # 1) Busca cliente
    try:
        members = await cloudgym.find_member_by_phone(phone)
    except Exception as e:
        logger.exception("Erro buscando cliente")
        return {"ok": False, "error": f"falha busca_cliente: {e}"}

    memberid: str | None = None
    if members:
        m = members[0]
        memberid = _extract_memberid(m)
        plan = _extract_plan(m)
        # Regra crítica n8n: só agenda se plan == TRIAL_PLAN_ID (218281)
        if plan and plan != catalog.TRIAL_PLAN_ID:
            return {
                "ok": False,
                "error": "ja_aluno",
                "mensagem_tecnica": "Cliente já é aluno matriculado — chame atendimento_humano.",
            }

    # 2) Cadastro se não existir
    if not memberid:
        if not nome_completo:
            return {"ok": False, "error": "falta_nome", "mensagem_tecnica": "Peça o nome completo e chame novamente."}
        try:
            novo = await cloudgym.create_customer(nome_completo, phone)
            memberid = _extract_memberid(novo)
            if not memberid:
                logger.warning("create_customer não retornou memberid: %s", novo)
                return {"ok": False, "error": f"falha_cadastro: {novo}"}
        except Exception as e:
            logger.exception("Erro no cadastro")
            return {"ok": False, "error": f"falha_cadastro: {e}"}

    # 3) Cria agendamento — tenta cada class_id do slot até um ser aceito
    last_err: Exception | None = None
    result = None
    class_id_usado: int | None = None
    for cid in class_ids:
        try:
            result = await cloudgym.create_attendance_v2(memberid, data_str, cid)
            class_id_usado = cid
            break
        except Exception as e:
            logger.debug("create_attendance_v2 falhou em class_id=%s: %s", cid, e)
            last_err = e
            continue

    if result is None or class_id_usado is None:
        return {"ok": False, "error": f"falha_agendamento: {last_err}"}

    class_id = class_id_usado

    # Salva dia_aula no SQLite (usado pelo job pós-trial D+1)
    try:
        dt = datetime.strptime(data_str, "%Y-%m-%d").date()
        await db.set_dia_aula(phone, dt.strftime("%d/%m/%Y"))
    except Exception:
        pass

    # Notifica recepção
    try:
        mod_display = modalidade or catalog.get_class_meta(class_id).get("modalidade", "?")
        alert_text = (
            f"\U0001f4c5 NOVA AULA EXPERIMENTAL\n"
            f"Modalidade: {mod_display}\n"
            f"Data/Hora: {data_str} {hora or '?'}\n"
            f"Aluno: {nome_completo or '(já cadastrado)'}\n"
            f"Telefone: {phone}\n"
            f"MemberId: {memberid} | ClassId: {class_id}"
        )
        await uazapi.send_text(settings.ALERT_PHONE, alert_text)
    except Exception as e:
        logger.warning("Falha ao notificar recepcao: %s", e)

    return {
        "ok": True,
        "modalidade": modalidade,
        "data": data_str,
        "hora": hora,
        "memberid": memberid,
        "class_id": class_id,
        "result": result,
    }


async def handle_atendimento_humano(phone: str, args: dict) -> dict:
    motivo = (args.get("motivo") or "").strip()[:120] or "Transferência para atendente"
    lead = await db.get_lead(phone) or {}
    nome = lead.get("nome") or phone

    alert_text = (
        f"\U0001f6a8 ATENDIMENTO HUMANO \U0001f6a8\n"
        f"Contato: {nome} ({phone})\n"
        f"Motivo: {motivo}"
    )
    try:
        await uazapi.send_text(settings.ALERT_PHONE, alert_text)
    except Exception as e:
        logger.warning("Falha ao enviar alerta de atendimento: %s", e)

    await db.set_modo_mudo(phone, True)
    return {"ok": True, "motivo": motivo}


HANDLERS = {
    "salva_nome": handle_salva_nome,
    "classifica_contato": handle_classifica_contato,
    "lista_horarios": handle_lista_horarios,
    "catalogo_horarios": handle_catalogo_horarios,
    "agenda_aula": handle_agenda_aula,
    "atendimento_humano": handle_atendimento_humano,
}


async def dispatch(name: str, phone: str, args: dict) -> dict:
    handler = HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"tool '{name}' desconhecida"}
    try:
        return await handler(phone, args or {})
    except Exception as e:
        logger.exception("Erro no handler da tool %s", name)
        return {"ok": False, "error": str(e)}
