"""Avisos al móvil por la app de Home Assistant.

- Descongelar: la víspera a las 21:00 (comidas-avisos.timer), si mañana toca una
  ración del congelador que aún no está marcada como descongelada en el panel.
- Borrador nuevo: el domingo, al acabar de generarlo (`python -m panel.generar --avisar`).

Un aviso que falla no rompe nada: se escribe en el journal y ya está.

Uso (en el servidor):
    python -m panel.avisos descongelar
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys

from . import config, db
from .ha import HA
from .mealie import TIPOS_PANEL, Mealie

_TIPO = {"lunch": "comida", "dinner": "cena"}
_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")


def _destino() -> str:
    return os.environ.get("HA_NOTIFY", config.AVISOS_NOTIFY)


def envia(titulo: str, mensaje: str, ha: HA | None = None) -> bool:
    """Manda el aviso. Devuelve si salió; sin destino configurado o con HA caído, no."""
    destino = _destino()
    if not destino:
        print(f"Sin HA_NOTIFY, no se avisa: {titulo} — {mensaje}")
        return False
    try:
        ha = ha or HA(os.environ["HA_URL"], os.environ["HA_TOKEN"], "")
        with ha:
            ha.notifica(destino, titulo, mensaje, os.environ.get("PANEL_URL", config.PANEL_URL))
    except Exception as e:  # noqa: BLE001 — un aviso perdido no debe tumbar el timer
        print(f"No se pudo avisar ({e}): {titulo} — {mensaje}", file=sys.stderr)
        return False
    print(f"Avisado: {titulo} — {mensaje}")
    return True


def _lista(cosas: list[str]) -> str:
    return cosas[0] if len(cosas) == 1 else ", ".join(cosas[:-1]) + " y " + cosas[-1]


# --- Descongelar ---------------------------------------------------------------------

def por_descongelar(m: Mealie, c: sqlite3.Connection, manana: dt.date) -> list[tuple[str, str]]:
    """(tipo, nombre) de las raciones del congelador de `manana` que siguen sin pasar
    al frigo. Igual que el bloque Descongelar del panel: plan aprobado y borrador."""
    frigo = db.descongelados(c, manana)
    del_cong = db.del_congelador(c, manana, manana)
    out = [(e["entryType"], (e.get("recipe") or {}).get("name") or e.get("title") or "")
           for e in m.plan(manana, manana)
           if e["entryType"] in TIPOS_PANEL and (e["date"], e["entryType"]) in del_cong]
    borrador = [f for f in db.borrador(c, manana, manana) if f["congelador"]]
    if borrador:
        recetas = {r["id"]: r for r in m.recetas()}
        out += [(f["tipo"], (recetas.get(f["receta_id"]) or {}).get("name", "")) for f in borrador]
    return sorted(((t, n) for t, n in out if t not in frigo), key=lambda x: x[0] != "lunch")


def mensaje_descongelar(platos: list[tuple[str, str]]) -> tuple[str, str]:
    return ("Pasa al frigo", _lista([f"{n} ({_TIPO[t]})" for t, n in platos]) + ", para mañana.")


def avisa_descongelar(m: Mealie, c: sqlite3.Connection, hoy: dt.date, ha: HA | None = None) -> bool:
    platos = por_descongelar(m, c, hoy + dt.timedelta(days=1))
    if not platos:
        print("Mañana no hay nada que descongelar")
        return False
    return envia(*mensaje_descongelar(platos), ha=ha)


# --- Borrador ---------------------------------------------------------------------

def mensaje_borrador(n: int, lunes: dt.date) -> tuple[str, str]:
    return ("Borrador nuevo",
            f"{n} platos para la semana del {lunes.day} de {_MESES[lunes.month - 1]}. Revísalo y apruébalo.")


def avisa_borrador(n: int, lunes: dt.date, ha: HA | None = None) -> bool:
    return envia(*mensaje_borrador(n, lunes), ha=ha) if n else False


def main() -> None:
    ap = argparse.ArgumentParser(description="Avisos al móvil por la app de HA")
    ap.add_argument("aviso", choices=["descongelar"])
    ap.parse_args()
    with Mealie(os.environ["MEALIE_URL"], os.environ["MEALIE_TOKEN"]) as m, db.conexion() as c:
        avisa_descongelar(m, c, dt.date.today())


if __name__ == "__main__":
    main()
