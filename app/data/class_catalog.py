"""
Catálogo estático de aulas da CloudGym (unit 2751 — Seven Academia).

Extraído do nó `id_aula` do fluxo n8n `agenda horario seven.json`. Cada modalidade
tem vários `class_id` — um por slot fixo (dia da semana + hora) da grade.

A descoberta de qual ID corresponde a qual (dia, hora) é feita pelo script
`scripts/test_cloudgym.py`, que gera `scripts/class_discovery.json`.
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Optional

# ---------------- Mapping principal ----------------

CLASS_IDS_BY_MODALITY: dict[str, list[int]] = {
    "muay_thai": [
        22241580, 17468573, 18713494, 18713493, 23321895, 23321894,
        18713528, 18713529, 18713540, 18713539, 18713514, 18713515,
    ],
    "seven_bike": [
        22775332, 22775331, 22775346, 22775347, 22775348, 22775399,
        22775400, 22775415, 22775414, 22775437, 22775436,
    ],
    "seven_cross": [
        17468232, 17468234, 22230667, 23579959, 17468415, 18957362,
        18957361, 17468694, 17468695, 21730459,
    ],
    "seven_pump": [
        18395067, 9968306, 18433531, 17540216, 17482885, 9968301,
        17586251, 17586252, 9968407, 18399314,
    ],
    "fitdance": [17468401, 17468402, 17468616, 17468617],
    "bike_move": [22775487, 22775474, 22775473],
    "muay_thai_kids": [19235802, 19235803],
    "seven_mais_bike": [23579965],
}

# Nome amigável (usado em mensagens para a Zoe / recepção)
DISPLAY_NAME: dict[str, str] = {
    "muay_thai": "Muay Thai Feminino",
    "seven_bike": "Seven Bike",
    "seven_cross": "Seven Cross",
    "seven_pump": "Seven Pump",
    "fitdance": "Fit Dance",
    "bike_move": "Bike Move",
    "muay_thai_kids": "Muay Thai Kids",
    "seven_mais_bike": "Seven Mais - Seven Bike",
}

# Aliases normalizados (sem acento, lowercase) → chave canônica
ALIASES: dict[str, str] = {
    "cross": "seven_cross",
    "seven cross": "seven_cross",
    "crossfit": "seven_cross",
    "bike": "seven_bike",
    "seven bike": "seven_bike",
    "rpm": "seven_bike",
    "spinning": "seven_bike",
    "bike move": "bike_move",
    "pump": "seven_pump",
    "seven pump": "seven_pump",
    "muay": "muay_thai",
    "muay thai": "muay_thai",
    "muaythai": "muay_thai",
    "muay thai feminino": "muay_thai",
    "muay thai kids": "muay_thai_kids",
    "muay kids": "muay_thai_kids",
    "fitdance": "fitdance",
    "fit dance": "fitdance",
    "danca": "fitdance",
    "dança": "fitdance",
}

# Capacidade máxima por modalidade (informativo — CloudGym é a verdade)
CAPACITY: dict[str, int] = {
    "seven_bike": 20,
    "bike_move": 20,
    "seven_cross": 12,
    "fitdance": 25,
    "muay_thai": 18,
    "muay_thai_kids": 10,
    "seven_pump": 20,
}

# Plano "trial" (aula experimental): só agenda se member.plan == TRIAL_PLAN_ID
TRIAL_PLAN_ID = "218281"
# planExtId usado no cadastro via POST /customer
TRIAL_PLAN_EXT_ID = "i4udpk54"

# Mapeia modalidade canônica → nome em UPPERCASE como vem de /config/classes.
API_NAME: dict[str, str] = {
    "muay_thai": "MUAY THAI",
    "seven_bike": "SEVEN BIKE",
    "seven_cross": "SEVEN CROSS",
    "seven_pump": "SEVEN PUMP",
    "fitdance": "FITDANCE",
    "bike_move": "BIKE MOVE",
    "muay_thai_kids": "MUAY THAI KIDS",
    "seven_mais_bike": "SEVEN MAIS",
}

# Grade OFICIAL da Academia Seven — fonte da verdade. Allowlist de aulas que
# podem ser oferecidas/agendadas. Fonte: admin CloudGym (screenshot 2026-04-23).
#
# Por que existe: CloudGym `/config/classes` marca cada classe como rodando em
# Seg/Ter/Qua/Qui indiscriminadamente (campo `days` noisy). Sem allowlist, a
# Zoe oferecia Muay Thai 06:00 na Segunda (só roda Ter/Qui) e coisas parecidas.
# Aulas fora desta grade NÃO são surfaced em `lista_horarios`.
GRADE_OFICIAL: dict[tuple[str, int], set[str]] = {
    ("seven_cross", 0): {"06:00", "16:15", "18:30"},
    ("seven_cross", 2): {"06:00", "16:15", "18:30"},
    ("seven_cross", 4): {"06:00", "16:15", "18:30"},

    ("seven_bike", 0): {"07:00", "18:30"},
    ("seven_bike", 1): {"06:00", "08:15", "17:15"},
    ("seven_bike", 2): {"07:00", "18:30"},
    ("seven_bike", 3): {"06:00", "08:15", "17:15"},
    ("seven_bike", 4): {"07:00"},

    ("seven_pump", 0): {"08:15", "17:15"},
    ("seven_pump", 1): {"07:00", "18:30"},
    ("seven_pump", 2): {"08:15", "17:15"},
    ("seven_pump", 3): {"07:00", "18:30"},
    ("seven_pump", 4): {"08:15", "17:15"},

    ("muay_thai", 0): {"08:00", "15:00", "19:00"},
    ("muay_thai", 1): {"06:00", "17:15", "18:15"},
    ("muay_thai", 2): {"08:00", "15:00", "19:00"},
    ("muay_thai", 3): {"06:00", "17:15", "18:15"},

    ("muay_thai_kids", 0): {"18:00"},
    ("muay_thai_kids", 2): {"18:00"},

    ("fitdance", 0): {"20:00"},
    ("fitdance", 1): {"17:00"},
    ("fitdance", 2): {"20:00"},
    ("fitdance", 3): {"17:00"},

    ("bike_move", 1): {"19:30"},
    ("bike_move", 3): {"19:30"},
    ("bike_move", 4): {"18:30"},
}

# Derivado de GRADE_OFICIAL — dias em que cada modalidade roda. Usado como
# hard-filter em lista_horarios (fail fast sem bater na API).
WEEKDAYS_BY_MODALITY: dict[str, set[int]] = {
    canon: {wd for (c, wd) in GRADE_OFICIAL if c == canon}
    for canon in {c for (c, _) in GRADE_OFICIAL}
}
# Seven Mais (programa para 3ª idade) — sábado; sem slot coletivo no CloudGym
# mas mantido pra compatibilidade com `resolve_modality`.
WEEKDAYS_BY_MODALITY["seven_mais_bike"] = {5}


def slots_for_weekday(canon: str, weekday: int) -> set[str]:
    """Horas HH:mm oficiais para (modalidade_canon, weekday). Vazio se não existe."""
    return GRADE_OFICIAL.get((canon, weekday), set())


# ---------------- Discovery (ID → modalidade, weekday, hora) ----------------

_DISCOVERY_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "class_discovery.json"
_discovery_cache: Optional[dict[str, dict]] = None


def _load_discovery() -> dict[str, dict]:
    global _discovery_cache
    if _discovery_cache is not None:
        return _discovery_cache
    if _DISCOVERY_PATH.exists():
        try:
            _discovery_cache = json.loads(_DISCOVERY_PATH.read_text(encoding="utf-8"))
        except Exception:
            _discovery_cache = {}
    else:
        _discovery_cache = {}
    return _discovery_cache


def get_class_meta(class_id: int | str) -> dict:
    """Retorna {modalidade, startTime, weekday} se descoberto; {} caso contrário."""
    return _load_discovery().get(str(class_id), {})


# ---------------- Weekday por class_id (gerado por scripts/discover_weekdays.py) -----

_WEEKDAYS_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "class_weekdays.json"
_weekdays_cache: Optional[dict[str, list[int]]] = None


def class_weekdays(class_id: int | str) -> Optional[list[int]]:
    """Weekdays (0=seg..6=dom) em que o class_id específico roda, conforme
    descoberto rodando `scripts/discover_weekdays.py` contra a CloudGym.

    Retorna `None` se o mapa não existe ou não tem info desse ID — nesse caso
    o caller deve aceitar o class_id conservadoramente (não regredir)."""
    global _weekdays_cache
    if _weekdays_cache is None:
        if _WEEKDAYS_PATH.exists():
            try:
                raw = json.loads(_WEEKDAYS_PATH.read_text(encoding="utf-8"))
                _weekdays_cache = {str(k): list(v) for k, v in raw.items()}
            except Exception:
                _weekdays_cache = {}
        else:
            _weekdays_cache = {}
    return _weekdays_cache.get(str(class_id))


# ---------------- Helpers ----------------

def _normalize(s: str) -> str:
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def resolve_modality(q: str) -> Optional[str]:
    """Resolve um termo livre para a chave canônica. Retorna None se não casar."""
    qn = _normalize(q)
    if not qn:
        return None
    # match exato
    if qn in ALIASES:
        return ALIASES[qn]
    # substring bidirecional
    for alias, canon in ALIASES.items():
        if alias in qn or qn in alias:
            return canon
    # chave canônica crua
    if qn.replace(" ", "_") in CLASS_IDS_BY_MODALITY:
        return qn.replace(" ", "_")
    return None


def ids_for_modality(q: str) -> list[int]:
    canon = resolve_modality(q)
    if not canon:
        return []
    return CLASS_IDS_BY_MODALITY.get(canon, [])


def ids_for_modality_and_weekday(q: str, weekday: int) -> list[int]:
    """Se houver discovery, filtra só os IDs cujo weekday bate. Senão, retorna todos."""
    canon = resolve_modality(q)
    if not canon:
        return []
    all_ids = CLASS_IDS_BY_MODALITY.get(canon, [])
    discovery = _load_discovery()
    filtered = []
    for cid in all_ids:
        meta = discovery.get(str(cid))
        if not meta or meta.get("weekday") is None:
            filtered.append(cid)  # sem info → mantém (paga o custo de checar)
        elif int(meta["weekday"]) == weekday:
            filtered.append(cid)
    return filtered or all_ids
