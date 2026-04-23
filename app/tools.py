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

SCHEMA_AVISA_MUSCULACAO = gtypes.FunctionDeclaration(
    name="avisa_recepcao_musculacao",
    description=(
        "Avisa a recepção que o lead virá para aula experimental de MUSCULAÇÃO "
        "em uma data/hora específica. Use APENAS para musculação — a modalidade é "
        "livre demanda (sem slot no CloudGym). NÃO chama a API de agendamento; "
        "apenas dispara o alerta para a recepção. Chame quando o lead INSISTIR "
        "em informar um dia+horário específico para experimental de musculação."
    ),
    parameters=gtypes.Schema(
        type=gtypes.Type.OBJECT,
        properties={
            "data": gtypes.Schema(type=gtypes.Type.STRING, description="yyyy-MM-dd"),
            "hora": gtypes.Schema(type=gtypes.Type.STRING, description="HH:mm"),
            "nome_completo": gtypes.Schema(
                type=gtypes.Type.STRING,
                description="Nome completo do lead (se ainda não cadastrado).",
            ),
        },
        required=["data", "hora"],
    ),
)

SCHEMA_CONSULTA_AVALIACAO = gtypes.FunctionDeclaration(
    name="consulta_avaliacao_fisica",
    description=(
        "Retorna as regras completas da avaliação física (horários disponíveis, "
        "custo, duração, exigências). Uso EXCLUSIVO para alunos já matriculados. "
        "NÃO oferecer para leads."
    ),
    parameters=gtypes.Schema(type=gtypes.Type.OBJECT, properties={}),
)

SCHEMA_CONSULTA_APP = gtypes.FunctionDeclaration(
    name="consulta_app_login",
    description=(
        "Retorna as instruções de login no app CloudGym (usuário e senha padrão). "
        "Uso EXCLUSIVO para alunos já matriculados."
    ),
    parameters=gtypes.Schema(type=gtypes.Type.OBJECT, properties={}),
)

SCHEMA_CONSULTA_PLANOS = gtypes.FunctionDeclaration(
    name="consulta_planos_detalhes",
    description=(
        "Retorna detalhes específicos sobre planos/valores que não estão na "
        "imagem [IMAGEM_PLANOS_VALORES]. Use quando precisar das regras finas "
        "(upgrade de aluno Musc+1 modalidade, descontos de renovação antecipada, "
        "plano familiar, pacote de aulas avulsas, diárias)."
    ),
    parameters=gtypes.Schema(
        type=gtypes.Type.OBJECT,
        properties={
            "topico": gtypes.Schema(
                type=gtypes.Type.STRING,
                description=(
                    "Um destes valores: 'upgrade_aluno' (Musc+modalidade), "
                    "'renovacao_desconto' (desconto por antecipação), "
                    "'familiar' (plano familiar), 'avulsas' (pacote de check-ins), "
                    "'diarias' (valor da diária), 'desconto_feminino' (Muay Thai/FitDance sem musc)."
                ),
            ),
        },
        required=["topico"],
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
        SCHEMA_AVISA_MUSCULACAO,
        SCHEMA_CONSULTA_AVALIACAO,
        SCHEMA_CONSULTA_APP,
        SCHEMA_CONSULTA_PLANOS,
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


async def handle_lista_horarios(phone: str, args: dict) -> dict:
    """Consulta /config/classes (cached), cruza com a grade oficial e retorna
    até 3 slots que REALMENTE rodam no dia pedido.

    Fluxo:
    1. Valida weekday contra WEEKDAYS_BY_MODALITY (fail fast, sem bater na API).
    2. Carrega catálogo (1 chamada HTTP, cache 1h).
    3. Filtra por nome da modalidade (campo `name`).
    4. Agrupa por `time` — cada slot pode ter múltiplos class_ids.
    5. Aplica GRADE_OFICIAL — descarta horas que não existem nessa (modalidade, weekday).
       CloudGym devolve `days` ruidoso (marca Seg/Ter/Qua/Qui em tudo), então o
       allowlist da grade é a fonte da verdade.
    6. Filtra slots em passado ou <30min no futuro.
    7. Retorna até 3 slots com {hora, class_ids}.
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

    # Filtro por weekday do class_id: CloudGym /config/classes devolve o
    # catálogo inteiro da semana sem distinguir dia. Sem esse filtro, Zoe
    # acabava oferecendo 17:15 Bike na Quarta — que só roda Ter/Qui.
    # O mapa é populado por scripts/discover_weekdays.py; IDs sem info no
    # mapa são mantidos (conservador — não regride se o discovery não rodou).
    target_wd = dt.weekday()
    filtered = []
    skipped_ids = []
    for c in aulas:
        wds = catalog.class_weekdays(c.get("id"))
        if wds is None or target_wd in wds:
            filtered.append(c)
        else:
            skipped_ids.append(c.get("id"))
    if skipped_ids:
        logger.info(
            "[lista_horarios] %s em %s: descartados %d IDs fora do weekday %d (%s)",
            modalidade, data_str, len(skipped_ids), target_wd, skipped_ids,
        )
    if not filtered:
        return {
            "ok": False,
            "error": "dia_sem_aula",
            "modalidade": catalog.DISPLAY_NAME.get(canon, canon),
            "mensagem_tecnica": f"{catalog.DISPLAY_NAME.get(canon, canon)} não roda em {data_str}.",
        }
    aulas = filtered

    # Agrupa por `time`
    by_time: dict[str, dict] = {}
    for c in aulas:
        hora = _hhmm(c.get("time"))
        entry = by_time.setdefault(hora, {"hora": hora, "class_ids": []})
        entry["class_ids"].append(int(c["id"]))

    # Allowlist — só sobrevivem horas que de fato rodam nessa (modalidade, weekday).
    allowed_horas = catalog.slots_for_weekday(canon, target_wd)
    if not allowed_horas:
        return {
            "ok": False,
            "error": "dia_sem_aula",
            "modalidade": catalog.DISPLAY_NAME.get(canon, canon),
            "mensagem_tecnica": f"{catalog.DISPLAY_NAME.get(canon, canon)} não roda em {data_str}.",
        }
    phantom = [h for h in by_time if h not in allowed_horas]
    if phantom:
        logger.info(
            "[lista_horarios] %s em %s: descartadas %d horas fora da grade oficial (%s)",
            modalidade, data_str, len(phantom), phantom,
        )
    by_time = {h: info for h, info in by_time.items() if h in allowed_horas}

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
        try:
            from datetime import datetime as _dt
            data_fmt = _dt.strptime(data_str, "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            data_fmt = data_str
        alert_text = (
            f"\U0001f4c5 NOVA AULA EXPERIMENTAL\n"
            f"Modalidade: {mod_display}\n"
            f"Data/Hora: {data_fmt} {hora or '?'}\n"
            f"Aluno: {nome_completo or '(já cadastrado)'}\n"
            f"Telefone: {phone}"
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

    alert_sent = False
    for tentativa in range(3):
        try:
            await uazapi.send_text(settings.ALERT_PHONE, alert_text)
            logger.info("Alerta de atendimento enviado com sucesso para recepção: %s", phone)
            alert_sent = True
            break
        except Exception as e:
            logger.error("Falha ao enviar alerta (tentativa %d/3): %s", tentativa + 1, e)
            if tentativa < 2:
                await asyncio.sleep(2)

    if not alert_sent:
        logger.critical("CRÍTICO: Alerta não foi enviado após 3 tentativas para %s", phone)

    await db.set_modo_mudo(phone, True)
    return {"ok": True, "motivo": motivo, "alert_sent": alert_sent}


async def handle_avisa_recepcao_musculacao(phone: str, args: dict) -> dict:
    """Alerta a recepção sobre aula experimental de musculação (livre demanda).

    Musculação não tem slot no CloudGym — é livre demanda. Quando o lead informa
    um dia+horário específico para experimental de musculação, apenas disparamos
    o alerta para a recepção no mesmo formato usado por `agenda_aula`.
    """
    data_str = (args.get("data") or "").strip()
    hora = (args.get("hora") or "").strip()
    nome_completo = (args.get("nome_completo") or "").strip()

    if not data_str or not hora:
        return {"ok": False, "error": "data e hora são obrigatórias"}

    # Validação leve da data
    try:
        dt = datetime.strptime(data_str, "%Y-%m-%d").date()
    except ValueError:
        return {"ok": False, "error": "data inválida — use yyyy-MM-dd"}

    if dt.weekday() == 6:
        return {"ok": False, "error": "academia não atende aula experimental aos domingos"}

    # Fallback: busca nome no SQLite se não veio
    if not nome_completo:
        lead = await db.get_lead(phone) or {}
        nome_completo = (lead.get("nome") or "").strip()

    try:
        from datetime import datetime as _dt
        data_fmt = _dt.strptime(data_str, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        data_fmt = data_str

    alert_text = (
        f"\U0001f4c5 NOVA AULA EXPERIMENTAL\n"
        f"Modalidade: Musculação (livre demanda)\n"
        f"Data/Hora: {data_fmt} {hora}\n"
        f"Aluno: {nome_completo or '(nome não informado)'}\n"
        f"Telefone: {phone}"
    )

    alert_sent = False
    for tentativa in range(3):
        try:
            await uazapi.send_text(settings.ALERT_PHONE, alert_text)
            logger.info("Alerta musculação enviado para recepção: %s", phone)
            alert_sent = True
            break
        except Exception as e:
            logger.warning("Falha ao alertar musculação (tentativa %d/3): %s", tentativa + 1, e)
            if tentativa < 2:
                await asyncio.sleep(2)

    # Salva dia_aula para o job pós-trial D+1
    try:
        await db.set_dia_aula(phone, data_fmt)
    except Exception:
        pass

    return {
        "ok": True,
        "modalidade": "Musculação",
        "data": data_str,
        "hora": hora,
        "alert_sent": alert_sent,
    }


# ---------------- handlers de consulta de base de conhecimento ----------------

_INFO_AVALIACAO = (
    "AVALIAÇÃO FÍSICA (somente para alunos matriculados):\n"
    "- Aluno matriculado mensal/semestral ou renovação semestral: 1 avaliação grátis.\n"
    "- Aluno mensal que quer nova avaliação: R$ 30,00.\n"
    "- Falta sem aviso: perde gratuidade; remarcação R$ 30,00.\n"
    "- Duração: máx 20 min. Comer com até 2h de antecedência; preferir shorts. "
    "NÃO pode treinar antes, só depois.\n"
    "- Horários disponíveis:\n"
    "  • Terça 19-21h\n"
    "  • Quarta 9-10h\n"
    "  • Quinta 19-21h\n"
    "  • Sexta 17-18h e 19-21h"
)

_INFO_APP = (
    "APP CloudGym — login:\n"
    "- Usuário: e-mail cadastrado no sistema.\n"
    "- Senha: data de aniversário completa sem pontuação (ddmmaaaa)."
)

_INFO_PLANOS = {
    "upgrade_aluno": (
        "UPGRADE DE ALUNO (Musculação + 1 modalidade):\n"
        "- Se quiser adicionar CROSS / MUAY THAI / FITDANCE → plano 'Modalidades Individuais'.\n"
        "- Se quiser adicionar SEVEN PUMP / SEVEN BIKE / BIKE MOVE → plano 'Coletivas'.\n"
        "- Se quiser várias aulas (Bike + Pump + FitDance, etc.) → plano 'Seven Gold' (acesso ilimitado)."
    ),
    "renovacao_desconto": (
        "DESCONTOS DE RENOVAÇÃO (plano semestral):\n"
        "- Renovar 15 a 8 dias antes do vencimento: 10% de desconto.\n"
        "- Renovar até 1 dia antes do vencimento: 5% de desconto.\n"
        "- Descontos NÃO são cumulativos com outras promoções ou convênios."
    ),
    "familiar": (
        "PLANO FAMILIAR:\n"
        "- 10% de desconto no plano semestral para pessoas da mesma família "
        "(ex: mãe e filho, marido e esposa) ou que compartilham a mesma renda.\n"
        "- Com desconto chega a ~R$ 87,92/mês."
    ),
    "avulsas": (
        "PACOTE DE AULAS AVULSAS (check-ins extras):\n"
        "- Público: leads que querem treinar poucos dias OU alunos que esgotaram "
        "os 6 check-ins semanais.\n"
        "- Valores: 2 check-ins por R$ 32,00 OU 4 check-ins por R$ 60,00.\n"
        "- Validade: 7 dias da data da compra.\n"
        "- Compra: APENAS na recepção (não pelo WhatsApp).\n"
        "- Pagamento: dinheiro, PIX ou cartão de débito."
    ),
    "diarias": (
        "DIÁRIAS:\n"
        "- 1º dia: R$ 30,00.\n"
        "- Demais dias: R$ 15,00."
    ),
    "desconto_feminino": (
        "DESCONTO EXCLUSIVO (Muay Thai Feminino / FitDance):\n"
        "- Alunas dessas modalidades que optarem por praticar APENAS a modalidade "
        "(sem usar musculação) têm direito ao valor de tabela de convênios "
        "(20% de desconto)."
    ),
}


async def handle_consulta_avaliacao_fisica(phone: str, args: dict) -> dict:
    return {"ok": True, "info": _INFO_AVALIACAO}


async def handle_consulta_app_login(phone: str, args: dict) -> dict:
    return {"ok": True, "info": _INFO_APP}


async def handle_consulta_planos_detalhes(phone: str, args: dict) -> dict:
    topico = (args.get("topico") or "").strip().lower()
    info = _INFO_PLANOS.get(topico)
    if not info:
        return {
            "ok": False,
            "error": f"tópico '{topico}' não reconhecido",
            "topicos_validos": list(_INFO_PLANOS.keys()),
        }
    return {"ok": True, "topico": topico, "info": info}


HANDLERS = {
    "salva_nome": handle_salva_nome,
    "classifica_contato": handle_classifica_contato,
    "lista_horarios": handle_lista_horarios,
    "catalogo_horarios": handle_catalogo_horarios,
    "agenda_aula": handle_agenda_aula,
    "atendimento_humano": handle_atendimento_humano,
    "avisa_recepcao_musculacao": handle_avisa_recepcao_musculacao,
    "consulta_avaliacao_fisica": handle_consulta_avaliacao_fisica,
    "consulta_app_login": handle_consulta_app_login,
    "consulta_planos_detalhes": handle_consulta_planos_detalhes,
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
