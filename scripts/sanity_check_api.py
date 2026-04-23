"""Valida se /admin/classattendancelist retorna algo diferente entre:
- um class_id em um dia em que ele RODA (grade positiva)
- o mesmo class_id em um dia em que NÃO roda (grade negativa)

class_id 22775346 = Seven Bike 07:00, grade Seg/Qua/Sex (weekdays 0, 2, 4).
- Próxima Seg (2026-04-27, wd=0): deve RODAR
- Próximo Ter (2026-04-28, wd=1): NÃO deve rodar
- Próxima Qua (2026-04-29, wd=2): deve RODAR
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import cloudgym  # noqa: E402


async def main() -> None:
    cases = [
        ("22775346", "2026-04-27", "Seg — deve RODAR"),
        ("22775346", "2026-04-28", "Ter — NÃO deve rodar"),
        ("22775346", "2026-04-29", "Qua — deve RODAR"),
        # class_id 22775347 suposto Qua — testando mesmas datas
        ("22775347", "2026-04-27", "Seg — 22775347"),
        ("22775347", "2026-04-28", "Ter — 22775347"),
        ("22775347", "2026-04-29", "Qua — 22775347"),
    ]
    for cid, dt, label in cases:
        try:
            r = await cloudgym.get_class_availability(dt, cid)
        except Exception as e:
            print(f"{cid} {dt} [{label}]: ERRO {e}")
            await asyncio.sleep(1)
            continue
        raw = json.dumps(r)[:400]
        if isinstance(r, list):
            n = len(r)
            keys = list(r[0].keys())[:8] if r else []
        else:
            n = "dict"
            keys = list(r.keys())[:8]
        print(f"{cid} {dt} [{label}]: type={type(r).__name__} n={n} keys={keys}")
        print(f"   raw[:400]={raw}")
        await asyncio.sleep(1.5)


if __name__ == "__main__":
    asyncio.run(main())
