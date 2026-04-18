"""
Inspetor manual do job `absent` (follow-up de alunos ausentes >3d).

Somente leitura — NÃO envia mensagem em hipótese alguma. Reutiliza
`app.followups.absent.collect_targets()` para garantir que a mesma lógica de
filtro (staff, ativos, dedup de attendance) seja aplicada.

Como usar:
  cd clientes/seven
  python -m scripts.alunos_ausentes                         # lista completa
  python -m scripts.alunos_ausentes --limit 20              # 20 primeiros
  python -m scripts.alunos_ausentes --csv ausentes.csv
  python -m scripts.alunos_ausentes --member-id 5307795     # debug cru 1 aluno
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.followups import absent  # noqa: E402
from app.services import cloudgym  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("alunos_ausentes")


async def _debug_single_member(member_id: str) -> None:
    print(f"[debug] GET /customer/attendance/{member_id}?size=5&sort=date,desc")
    items = await cloudgym.get_member_attendance(member_id, size=5, sort="date,desc")
    print(f"[debug] {len(items)} registro(s)")
    print(json.dumps(items, indent=2, ensure_ascii=False, default=str))


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, help="Processa só os primeiros N membros ativos")
    p.add_argument("--csv", help="Salva resultado em CSV")
    p.add_argument("--member-id", help="DEBUG: imprime attendance cru de 1 aluno e sai")
    args = p.parse_args()

    if args.member_id:
        await _debug_single_member(args.member_id)
        return

    targets = await absent.collect_targets(limit=args.limit)
    targets.sort(key=lambda t: t[1], reverse=True)  # mais dias ausente primeiro

    print(f"\n[+] {len(targets)} aluno(s) ausente(s) há mais de {absent.DIAS_CORTE} dias:\n")
    rows = []
    for member, dias in targets:
        mid = member.get("memberid") or member.get("memberId") or member.get("id") or ""
        nome = member.get("name") or ""
        fone = member.get("cellphonenumber") or member.get("phonenumber") or ""
        dias_str = "nunca" if dias >= 999 else str(dias)
        print(f"  {mid:<10} {nome:<45} {fone:<16} dias: {dias_str}")
        rows.append({
            "memberid": mid,
            "nome": nome,
            "telefone": fone,
            "dias_sem_frequentar": dias_str,
        })

    if args.csv:
        out = Path(args.csv)
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["memberid", "nome", "telefone", "dias_sem_frequentar"])
            w.writeheader()
            w.writerows(rows)
        print(f"\n[+] CSV salvo em {out} ({len(rows)} linhas)")


if __name__ == "__main__":
    asyncio.run(main())
