"""
Tools expostas ao Gemini via function calling.

Cada tool tem:
- uma FunctionDeclaration (schema que vai ao modelo)
- um handler assíncrono (executado localmente quando o modelo chama a função)

O handler retorna um dict que será entregue ao modelo como `function_response`.
O modelo então decide se chama outra tool ou se gera texto final.
"""
import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any

from google.genai import types as gtypes

from app import db
from app.config import settings
from app.services import cloudgym, uazapi

logger = logging.getLogger(__name__)


# ---------------- helpers ----------------

def _normalize(s: str) -> str:
    """Remove acentos e lowercase para fuzzy match de nome de modalidade."""
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


_MOD_ALIASES = {
    "cross": ["cross", "seven cross", "crossfit"],
    "bike": ["bike", "seven bike", "rpm", "spinning", "bike move"],
    "pump": ["pump", "seven pump"],
    "muay": ["muay thai", "muaythai", "muay thai feminino", "muay thai kids"],
    "fitdance": ["fitdance", "fit dance"],
    "musculacao": ["musculacao", "musculação", "musc"],
}


def _match_modalidade(q: str) -> str:
    qn = _normalize(q)
    for key, aliases in _MOD_ALIASES.items():
        for a in aliases:
            if _normalize(a) in qn or qn in _normalize(a):
                return key
    return qn


async def _find_class_id(nome_aula: str) -> tuple[str, str]:
    """Retorna (class_id, class_name_canonico) buscando em /config/classes."""
    classes = await cloudgym.list_classes()
    nq = _normalize(nome_aula)
    for c in classes:
        name = c.get("name") or c.get("className") or ""
        if _normalize(name) == nq or _match_modalidade(name) == _match_modalidade(nome_aula):
            return str(c.get("id") or c.get("classId") or ""), name
    # fallback: retorna primeiro match parcial
    for c in classes:
        name = c.get("name") or c.get("className") or ""
        if nq in _normalize(name):
            return str(c.get("id") or c.get("classId") or ""), name
    return "", ""


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
        "Lista horários disponíveis de uma modalidade numa data específica. "
        "Use para propor 3 opções de aula experimental ao lead."
    ),
    parameters=gtypes.Schema(
        type=gtypes.Type.OBJECT,
        properties={
            "modalidade": gtypes.Schema(
                type=gtypes.Type.STRING,
                description="Ex: 'seven cross', 'muay thai', 'fitdance', 'seven bike', 'seven pump'",
            ),
            "data": gtypes.Schema(
                type=gtypes.Type.STRING,
                description="Data no formato yyyy-MM-dd. NUNCA domingo.",
            ),
        },
        required=["modalidade", "data"],
    ),
)

SCHEMA_AGENDA_AULA = gtypes.FunctionDeclaration(
    name="agenda_aula",
    description=(
        "Efetiva o agendamento de aula experimental no CloudGym. "
        "Só chame após o lead confirmar dia, hora e fornecer o nome completo."
    ),
    parameters=gtypes.Schema(
        type=gtypes.Type.OBJECT,
        properties={
            "modalidade": gtypes.Schema(type=gtypes.Type.STRING),
            "data": gtypes.Schema(type=gtypes.Type.STRING, description="yyyy-MM-dd"),
            "hora": gtypes.Schema(type=gtypes.Type.STRING, description="HH:mm"),
            "nome_completo": gtypes.Schema(type=gtypes.Type.STRING),
            "telefone": gtypes.Schema(
                type=gtypes.Type.STRING,
                description="Telefone do lead (somente dígitos). Pode deixar vazio — o sistema preenche.",
            ),
        },
        required=["modalidade", "data", "hora", "nome_completo"],
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


async def handle_lista_horarios(phone: str, args: dict) -> dict:
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

    try:
        class_id, class_name = await _find_class_id(modalidade)
    except Exception as e:
        logger.exception("Erro ao listar classes")
        return {"ok": False, "error": f"falha consultando catálogo: {e}"}

    if not class_id:
        return {"ok": False, "error": f"modalidade '{modalidade}' não encontrada no catálogo"}

    try:
        availability = await cloudgym.get_class_availability(data_str, class_id)
    except Exception as e:
        logger.exception("Erro ao buscar disponibilidade")
        return {"ok": False, "error": f"falha consultando disponibilidade: {e}"}

    slots = []
    items = availability if isinstance(availability, list) else availability.get("items", [])
    for item in items or []:
        hora = item.get("startTime") or item.get("hora") or item.get("start") or ""
        vagas = item.get("availableSlots") or item.get("vagas") or item.get("remaining") or None
        if hora:
            slots.append({"hora": hora, "vagas": vagas})

    return {
        "ok": True,
        "modalidade": class_name or modalidade,
        "class_id": class_id,
        "data": data_str,
        "slots": slots[:6],
    }


async def handle_agenda_aula(phone: str, args: dict) -> dict:
    modalidade = (args.get("modalidade") or "").strip()
    data_str = (args.get("data") or "").strip()
    hora = (args.get("hora") or "").strip()
    nome_completo = (args.get("nome_completo") or "").strip()
    telefone = (args.get("telefone") or phone).strip()

    if not all([modalidade, data_str, hora, nome_completo]):
        return {"ok": False, "error": "campos obrigatórios faltando"}

    try:
        class_id, class_name = await _find_class_id(modalidade)
        if not class_id:
            return {"ok": False, "error": f"modalidade '{modalidade}' não encontrada"}

        payload = {
            "unitId": settings.CLOUDGYM_UNIT_ID,
            "classId": class_id,
            "date": data_str,
            "startTime": hora,
            "customerName": nome_completo,
            "customerPhone": telefone,
        }
        result = await cloudgym.create_attendance(payload)
    except Exception as e:
        logger.exception("Erro ao criar attendance")
        return {"ok": False, "error": f"falha ao agendar: {e}"}

    # Grava dia_aula no SQLite no formato dd/MM/yyyy (usado pelo job pós-trial D+1)
    try:
        dt = datetime.strptime(data_str, "%Y-%m-%d").date()
        await db.set_dia_aula(phone, dt.strftime("%d/%m/%Y"))
    except Exception:
        pass

    # Notifica recepção
    try:
        alert_text = (
            f"\U0001f4c5 NOVA AULA EXPERIMENTAL\n"
            f"Modalidade: {class_name or modalidade}\n"
            f"Data/Hora: {data_str} {hora}\n"
            f"Aluno: {nome_completo}\n"
            f"Telefone: {telefone}"
        )
        await uazapi.send_text(settings.ALERT_PHONE, alert_text)
    except Exception as e:
        logger.warning("Falha ao notificar recepcao: %s", e)

    return {"ok": True, "modalidade": class_name or modalidade, "data": data_str, "hora": hora, "result": result}


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
