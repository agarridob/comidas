"""Estado propio del panel en SQLite: lo que no es de Mealie ni de HA.

Mealie sigue siendo la fuente de verdad de recetas, plan aprobado y valoraciones;
HA, de la lista de la compra. Aquí solo vive el estado del panel:

- borrador:      platos propuestos y aún no aprobados (aprobar = crearlos en Mealie).
                 `congelador = 1`: es una ración que sale del congelador.
- descongelado:  comidas cuyo congelado ya está en el frigo, por (fecha, tipo).
- compra_hecha:  platos cuyos ingredientes ya se pasaron a la lista de la compra.
- congelador:    inventario: raciones congeladas por receta.
- del_congelador: platos del plan aprobado que son una ración del congelador.
- procesado:     platos ya pasados por el inventario (para no contarlos dos veces).
- hueco_libre:   huecos quitados a mano (festivo, se come fuera…): el borrador no los
                 rellena.

El fichero está en /var/lib/comidas (StateDirectory de systemd); conviene que entre
en el backup del servidor.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS borrador (
    id        INTEGER PRIMARY KEY,
    fecha     TEXT NOT NULL,           -- ISO, YYYY-MM-DD
    tipo      TEXT NOT NULL,           -- lunch | dinner
    receta_id TEXT NOT NULL,
    creado    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS borrador_fecha ON borrador (fecha, tipo);

-- Platos del plan cuyos ingredientes ya se pasaron a la lista de la compra.
CREATE TABLE IF NOT EXISTS compra_hecha (
    fecha     TEXT NOT NULL,
    tipo      TEXT NOT NULL,
    receta_id TEXT NOT NULL,
    hecho     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (fecha, tipo, receta_id)
);

-- Inventario del congelador: raciones por receta.
CREATE TABLE IF NOT EXISTS congelador (
    receta_id   TEXT PRIMARY KEY,
    raciones    INTEGER NOT NULL,
    actualizado TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Platos aprobados (en Mealie) que son una ración del congelador, no algo que cocinar.
CREATE TABLE IF NOT EXISTS del_congelador (
    fecha     TEXT NOT NULL,
    tipo      TEXT NOT NULL,
    receta_id TEXT NOT NULL,
    PRIMARY KEY (fecha, tipo)
);

-- Platos del plan que ya pasaron y se aplicaron al inventario (cocinado → entra,
-- del congelador → sale). Evita contarlos dos veces.
CREATE TABLE IF NOT EXISTS procesado (
    fecha     TEXT NOT NULL,
    tipo      TEXT NOT NULL,
    receta_id TEXT NOT NULL,
    PRIMARY KEY (fecha, tipo, receta_id)
);

-- Huecos (fecha, tipo) quitados a mano: el borrador no los rellena, ni al regenerar.
CREATE TABLE IF NOT EXISTS hueco_libre (
    fecha  TEXT NOT NULL,
    tipo   TEXT NOT NULL,
    PRIMARY KEY (fecha, tipo)
);

CREATE TABLE IF NOT EXISTS descongelado (
    fecha  TEXT NOT NULL,
    tipo   TEXT NOT NULL,
    hecho  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (fecha, tipo)
);
"""


def ruta() -> Path:
    return Path(os.environ.get("COMIDAS_DB", "/var/lib/comidas/comidas.db"))


@contextmanager
def conexion(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Conexión con el esquema creado y transacción: commit al salir, rollback si falla."""
    path = Path(path or ruta())
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as c:
        c.row_factory = sqlite3.Row
        c.executescript(_ESQUEMA)
        _migra(c)
        with c:
            yield c


def _migra(c: sqlite3.Connection) -> None:
    """Columnas añadidas después de crear la base de datos en producción."""
    columnas = {f["name"] for f in c.execute("PRAGMA table_info(borrador)")}
    if "congelador" not in columnas:
        c.execute("ALTER TABLE borrador ADD COLUMN congelador INTEGER NOT NULL DEFAULT 0")
        c.commit()


# --- Borrador --------------------------------------------------------------------

def borrador(c: sqlite3.Connection, desde: dt.date, hasta: dt.date) -> list[dict]:
    filas = c.execute(
        "SELECT id, fecha, tipo, receta_id, congelador FROM borrador WHERE fecha BETWEEN ? AND ? ORDER BY fecha, tipo",
        (desde.isoformat(), hasta.isoformat()),
    )
    return [dict(f) for f in filas]


def borrador_uno(c: sqlite3.Connection, id_: int) -> dict | None:
    f = c.execute("SELECT id, fecha, tipo, receta_id, congelador FROM borrador WHERE id = ?", (id_,)).fetchone()
    return dict(f) if f else None


def mete_borrador(c: sqlite3.Connection, fecha: dt.date, tipo: str, receta_id: str, congelador: bool = False) -> dict:
    cur = c.execute("INSERT INTO borrador (fecha, tipo, receta_id, congelador) VALUES (?, ?, ?, ?)",
                    (fecha.isoformat(), tipo, receta_id, int(congelador)))
    return {"id": cur.lastrowid, "fecha": fecha.isoformat(), "tipo": tipo, "receta_id": receta_id,
            "congelador": int(congelador)}


def cambia_borrador(c: sqlite3.Connection, id_: int, receta_id: str) -> None:
    """Otra receta para ese hueco: deja de ser una ración del congelador."""
    c.execute("UPDATE borrador SET receta_id = ?, congelador = 0 WHERE id = ?", (receta_id, id_))


def borra_borrador(c: sqlite3.Connection, desde: dt.date, hasta: dt.date | None = None) -> int:
    hasta = hasta or dt.date.max
    return c.execute("DELETE FROM borrador WHERE fecha BETWEEN ? AND ?",
                     (desde.isoformat(), hasta.isoformat())).rowcount


def borra_borrador_id(c: sqlite3.Connection, id_: int) -> None:
    c.execute("DELETE FROM borrador WHERE id = ?", (id_,))


# --- Huecos libres -----------------------------------------------------------------

def huecos_libres(c: sqlite3.Connection, desde: dt.date, hasta: dt.date) -> set[tuple[str, str]]:
    filas = c.execute("SELECT fecha, tipo FROM hueco_libre WHERE fecha BETWEEN ? AND ?",
                      (desde.isoformat(), hasta.isoformat()))
    return {(f["fecha"], f["tipo"]) for f in filas}


def marca_libre(c: sqlite3.Connection, fecha: str, tipo: str, libre: bool) -> None:
    if libre:
        c.execute("INSERT OR IGNORE INTO hueco_libre (fecha, tipo) VALUES (?, ?)", (fecha, tipo))
    else:
        c.execute("DELETE FROM hueco_libre WHERE fecha = ? AND tipo = ?", (fecha, tipo))


# --- Descongelado -----------------------------------------------------------------

def descongelados(c: sqlite3.Connection, fecha: dt.date) -> set[str]:
    """Tipos (lunch/dinner) de esa fecha que ya están en el frigo."""
    return {f["tipo"] for f in c.execute("SELECT tipo FROM descongelado WHERE fecha = ?", (fecha.isoformat(),))}


def marca_descongelado(c: sqlite3.Connection, fecha: dt.date, tipo: str, hecho: bool) -> None:
    if hecho:
        c.execute("INSERT OR IGNORE INTO descongelado (fecha, tipo) VALUES (?, ?)", (fecha.isoformat(), tipo))
    else:
        c.execute("DELETE FROM descongelado WHERE fecha = ? AND tipo = ?", (fecha.isoformat(), tipo))


# --- Compra -----------------------------------------------------------------------

def compra_hecha(c: sqlite3.Connection, desde: dt.date, hasta: dt.date) -> set[tuple[str, str, str]]:
    filas = c.execute("SELECT fecha, tipo, receta_id FROM compra_hecha WHERE fecha BETWEEN ? AND ?",
                      (desde.isoformat(), hasta.isoformat()))
    return {(f["fecha"], f["tipo"], f["receta_id"]) for f in filas}


def marca_compra_hecha(c: sqlite3.Connection, platos: list[tuple[str, str, str]]) -> None:
    c.executemany("INSERT OR IGNORE INTO compra_hecha (fecha, tipo, receta_id) VALUES (?, ?, ?)", platos)


# --- Congelador -------------------------------------------------------------------

def congelador(c: sqlite3.Connection) -> dict[str, int]:
    """receta_id -> raciones (solo las que tienen alguna)."""
    return {f["receta_id"]: f["raciones"] for f in c.execute("SELECT receta_id, raciones FROM congelador WHERE raciones > 0")}


def suma_congelador(c: sqlite3.Connection, receta_id: str, delta: int) -> int:
    """Añade (o quita, con delta negativo) raciones. Nunca baja de 0. Devuelve las que quedan."""
    actual = (c.execute("SELECT raciones FROM congelador WHERE receta_id = ?", (receta_id,)).fetchone() or {"raciones": 0})["raciones"]
    nuevo = max(0, actual + delta)
    if nuevo:
        c.execute("INSERT INTO congelador (receta_id, raciones) VALUES (?, ?) "
                  "ON CONFLICT(receta_id) DO UPDATE SET raciones = excluded.raciones, actualizado = datetime('now')",
                  (receta_id, nuevo))
    else:
        c.execute("DELETE FROM congelador WHERE receta_id = ?", (receta_id,))
    return nuevo


def del_congelador(c: sqlite3.Connection, desde: dt.date, hasta: dt.date) -> set[tuple[str, str]]:
    """(fecha, tipo) de los platos aprobados que son una ración del congelador."""
    filas = c.execute("SELECT fecha, tipo FROM del_congelador WHERE fecha BETWEEN ? AND ?",
                      (desde.isoformat(), hasta.isoformat()))
    return {(f["fecha"], f["tipo"]) for f in filas}


def marca_del_congelador(c: sqlite3.Connection, fecha: str, tipo: str, receta_id: str) -> None:
    c.execute("INSERT OR REPLACE INTO del_congelador (fecha, tipo, receta_id) VALUES (?, ?, ?)", (fecha, tipo, receta_id))


def borra_del_congelador(c: sqlite3.Connection, fecha: str, tipo: str) -> None:
    c.execute("DELETE FROM del_congelador WHERE fecha = ? AND tipo = ?", (fecha, tipo))


def procesados(c: sqlite3.Connection, desde: dt.date, hasta: dt.date) -> set[tuple[str, str, str]]:
    filas = c.execute("SELECT fecha, tipo, receta_id FROM procesado WHERE fecha BETWEEN ? AND ?",
                      (desde.isoformat(), hasta.isoformat()))
    return {(f["fecha"], f["tipo"], f["receta_id"]) for f in filas}


def marca_procesado(c: sqlite3.Connection, fecha: str, tipo: str, receta_id: str) -> None:
    c.execute("INSERT OR IGNORE INTO procesado (fecha, tipo, receta_id) VALUES (?, ?, ?)", (fecha, tipo, receta_id))


# --- Posponer -----------------------------------------------------------------------

def _desplaza_tabla(c: sqlite3.Connection, tabla: str, extra: tuple[str, ...], tipo: str, desde: dt.date, signo: str) -> None:
    """Corre `signo` las filas de `tabla` de ese tipo desde `desde`. Las tablas tienen
    clave con la fecha: se reescriben en bloque para no chocar a mitad del UPDATE."""
    cols = ", ".join(("fecha",) + extra)
    filas = c.execute(f"SELECT {cols} FROM {tabla} WHERE tipo = ? AND fecha >= ?", (tipo, desde.isoformat())).fetchall()
    c.execute(f"DELETE FROM {tabla} WHERE tipo = ? AND fecha >= ?", (tipo, desde.isoformat()))
    marcas = ", ".join(["date(?, ?)", "?"] + ["?"] * len(extra))
    c.executemany(f"INSERT OR IGNORE INTO {tabla} (fecha, tipo{''.join(', ' + e for e in extra)}) VALUES ({marcas})",
                  [(f["fecha"], signo, tipo, *(f[e] for e in extra)) for f in filas])


def desplaza(c: sqlite3.Connection, tipo: str, desde: dt.date, dias: int) -> None:
    """Al posponer, el borrador, lo descongelado, lo ya pasado a la compra y las
    raciones del congelador de ese tipo desde `desde` se corren igual que el plan de
    Mealie: el estado sigue al plato."""
    signo = f"{dias:+d} days"
    c.execute("UPDATE borrador SET fecha = date(fecha, ?) WHERE tipo = ? AND fecha >= ?",
              (signo, tipo, desde.isoformat()))
    _desplaza_tabla(c, "descongelado", (), tipo, desde, signo)
    _desplaza_tabla(c, "compra_hecha", ("receta_id",), tipo, desde, signo)
    _desplaza_tabla(c, "del_congelador", ("receta_id",), tipo, desde, signo)
