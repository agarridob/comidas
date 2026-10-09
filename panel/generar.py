"""Generación del borrador semanal (sorteo ponderado por valoración).

El borrador vive en la base de datos del panel (panel/db.py), no en Mealie: el plan
de Mealie solo contiene lo aprobado. El panel muestra el borrador mezclado con el
plan; "Cambiar" sortea otra receta para un hueco y "Aprobar" crea las entradas en
Mealie y vacía el borrador.

Uso (en el servidor; lo lanza el timer del domingo):
    python -m panel.generar               # semana que empieza el próximo lunes
    python -m panel.generar --lunes 2026-10-05
    python -m panel.generar --avisar      # y avisa al móvil (lo usa el timer)
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import sqlite3
from collections import Counter

from . import avisos, config, db
from .mealie import Mealie


def _slugs(receta: dict) -> set[str]:
    return {t["slug"] for t in receta.get("tags") or []}


def peso(receta: dict) -> float:
    estrellas = receta.get("rating") or config.ESTRELLAS_SIN_VALORAR
    return float(estrellas) ** 2


def proximo_lunes(hoy: dt.date) -> dt.date:
    return hoy + dt.timedelta(days=7 - hoy.weekday())


def huecos(lunes: dt.date, desde: dt.date) -> list[tuple[dt.date, str]]:
    """(fecha, tipo) a planificar en la semana de `lunes`, sin días anteriores a `desde`."""
    out = []
    for dia, tipos in sorted(config.HUECOS.items()):
        fecha = lunes + dt.timedelta(days=dia)
        if fecha >= desde:
            out.extend((fecha, t) for t in tipos)
    return out


def _cabe(receta: dict, cuenta: Counter) -> bool:
    return all(cuenta[s] < config.MAXIMOS[s] for s in _slugs(receta) if s in config.MAXIMOS)


def _sortea(candidatas: list[dict], rng: random.Random) -> dict | None:
    if not candidatas:
        return None
    return rng.choices(candidatas, weights=[peso(r) for r in candidatas])[0]


def sirve_para(receta: dict, tipo: str) -> bool:
    """¿La receta vale para ese hueco? Según su categoría de Mealie (comida / cena).
    Sin ninguna de las dos categorías, vale para cualquiera."""
    cats = {c["slug"] for c in receta.get("recipeCategory") or []} & set(config.CATEGORIA.values())
    return not cats or config.CATEGORIA[tipo] in cats


def elige(recetas: list[dict], tipos: list[str], fijas: list[dict], recientes: set[str],
          rng: random.Random) -> list[dict | None]:
    """Elige una receta distinta para cada hueco de `tipos` (lunch/dinner), alineada
    con él, de modo que junto con `fijas` (ya en la semana) se cumplan mínimos y
    máximos. La categoría (comida/cena) nunca se relaja: antes queda el hueco a None.
    Primero se cubren los mínimos, luego se rellena; si faltan recetas, se relaja
    primero no repetir las recientes y después las reglas de variedad."""
    usadas = {r["id"] for r in fijas}
    cuenta = Counter(s for r in fijas for s in _slugs(r))
    out: list[dict | None] = [None] * len(tipos)

    def libres(tipo: str | None = None) -> list[int]:
        return [i for i, t in enumerate(tipos) if out[i] is None and (tipo is None or t == tipo)]

    def pool(tipo: str | None, *, etiqueta: str | None = None, sin_recientes=True, con_reglas=True) -> list[dict]:
        return [
            r for r in recetas
            if r["id"] not in usadas
            and (tipo is None or sirve_para(r, tipo))
            and (not sin_recientes or r["id"] not in recientes)
            and (etiqueta is None or etiqueta in _slugs(r))
            and (not con_reglas or _cabe(r, cuenta))
        ]

    def toma(r: dict, i: int) -> None:
        out[i] = r
        usadas.add(r["id"])
        cuenta.update(_slugs(r))

    # Mínimos: la receta puede ir a cualquier tipo con hueco libre que le sirva;
    # si le sirven los dos, al que tenga más huecos libres.
    tipos_libres = lambda: {tipos[i] for i in libres()}  # noqa: E731
    for etiqueta, minimo in config.MINIMOS.items():
        while cuenta[etiqueta] < minimo and libres():
            cands = [r for r in pool(None, etiqueta=etiqueta) if any(sirve_para(r, t) for t in tipos_libres())]
            cands = cands or [r for r in pool(None, etiqueta=etiqueta, sin_recientes=False)
                              if any(sirve_para(r, t) for t in tipos_libres())]
            r = _sortea(cands, rng)
            if r is None:
                break
            tipo = max((t for t in tipos_libres() if sirve_para(r, t)), key=lambda t: len(libres(t)))
            toma(r, rng.choice(libres(tipo)))
    # Relleno, hueco a hueco en orden aleatorio.
    pendientes = libres()
    rng.shuffle(pendientes)
    for i in pendientes:
        t = tipos[i]
        r = (_sortea(pool(t), rng)
             or _sortea(pool(t, sin_recientes=False), rng)
             or _sortea(pool(t, sin_recientes=False, con_reglas=False), rng))
        if r is not None:   # si no, el hueco se queda libre
            toma(r, i)
    return out


# --- Congelador ------------------------------------------------------------------

def es_congelable(receta: dict | None) -> bool:
    return bool(receta) and config.ETIQUETA_CONGELABLE in _slugs(receta)


def raciones(receta: dict | None) -> int:
    return max(1, int((receta or {}).get("recipeServings") or 1))


def coloca_congelador(huecos_: list[tuple[dt.date, str]], disponibles: dict[str, int],
                      recetas: dict[str, dict], rng: random.Random) -> dict[int, dict]:
    """Reparte las raciones congeladas en los huecos (índice -> receta). Cada ración va a
    un hueco cuya categoría le sirva, procurando no repetir día con la misma receta.
    Lo que no cabe se queda en el congelador para la semana siguiente."""
    libres = set(range(len(huecos_)))
    out: dict[int, dict] = {}
    for rid, n in sorted(disponibles.items()):
        r = recetas.get(rid)
        if r is None:
            continue
        dias: set[dt.date] = set()
        for _ in range(n):
            cands = [i for i in libres if sirve_para(r, huecos_[i][1])]
            otro_dia = [i for i in cands if huecos_[i][0] not in dias]
            if not cands:
                break
            i = rng.choice(otro_dia or cands)
            out[i] = r
            libres.discard(i)
            dias.add(huecos_[i][0])
    return out


# --- Contra Mealie y la base de datos del panel -------------------------------------

def _recientes(m: Mealie, lunes: dt.date) -> set[str]:
    desde = lunes - dt.timedelta(days=config.DIAS_SIN_REPETIR)
    return {e["recipeId"] for e in m.plan(desde, lunes - dt.timedelta(days=1)) if e.get("recipeId")}


def _semana(lunes: dt.date) -> tuple[dt.date, dt.date]:
    return lunes, lunes + dt.timedelta(days=6)


def sincroniza(m: Mealie, c: sqlite3.Connection, hoy: dt.date) -> None:
    """Aplica al inventario los platos de los últimos 14 días que ya pasaron: un
    congelable cocinado mete (raciones - 1) al congelador; una ración del congelador
    comida, saca 1. Cada plato se procesa una sola vez."""
    desde, hasta = hoy - dt.timedelta(days=14), hoy - dt.timedelta(days=1)
    hechos = db.procesados(c, desde, hasta)
    del_cong = db.del_congelador(c, desde, hasta)
    for e in m.plan(desde, hasta):
        clave = (e["date"], e["entryType"], e.get("recipeId"))
        if not e.get("recipeId") or clave in hechos:
            continue
        if (e["date"], e["entryType"]) in del_cong:
            db.suma_congelador(c, e["recipeId"], -1)
        elif es_congelable(e.get("recipe")):
            db.suma_congelador(c, e["recipeId"], raciones(e["recipe"]) - 1)
        db.marca_procesado(c, *clave)


def disponible(m: Mealie, c: sqlite3.Connection, lunes: dt.date, hoy: dt.date) -> dict[str, int]:
    """Raciones que habrá en el congelador para la semana de `lunes`: lo que hay, más lo
    que se va a cocinar antes (congelables planificados entre hoy y el domingo
    anterior), menos lo que ya está planificado para comerse hasta el final de esa
    semana (aprobado o en borrador)."""
    stock = dict(db.congelador(c))
    domingo = lunes + dt.timedelta(days=6)

    def suma(rid: str, n: int) -> None:
        stock[rid] = stock.get(rid, 0) + n

    if hoy <= domingo:
        del_cong = db.del_congelador(c, hoy, domingo)
        for e in m.plan(hoy, domingo):
            if not e.get("recipeId"):
                continue
            if (e["date"], e["entryType"]) in del_cong:
                suma(e["recipeId"], -1)
            elif e["date"] < lunes.isoformat() and es_congelable(e.get("recipe")):
                suma(e["recipeId"], raciones(e["recipe"]) - 1)
        recetas = {r["id"]: r for r in m.recetas()}
        for b in db.borrador(c, hoy, lunes - dt.timedelta(days=1)):
            if b["congelador"]:
                suma(b["receta_id"], -1)
            elif es_congelable(recetas.get(b["receta_id"])):
                suma(b["receta_id"], raciones(recetas[b["receta_id"]]) - 1)
    return {k: v for k, v in stock.items() if v > 0}


def genera(m: Mealie, c: sqlite3.Connection, lunes: dt.date, hoy: dt.date,
           rng: random.Random | None = None) -> list[dict]:
    """Genera (o regenera) el borrador de la semana de `lunes` en la base de datos.
    Primero coloca lo que hay en el congelador; el resto se sortea. Lo ya aprobado (en
    Mealie) se respeta y cuenta para las reglas; el borrador anterior de esa semana se
    sustituye."""
    rng = rng or random.Random()
    sincroniza(m, c, hoy)
    aprobadas = m.plan(*_semana(lunes))
    db.borra_borrador(c, *_semana(lunes))
    ocupados = {(e["date"], e["entryType"]) for e in aprobadas}
    libres = [(f, t) for f, t in huecos(lunes, hoy) if (f.isoformat(), t) not in ocupados]

    recetas = m.recetas()
    por_id = {r["id"]: r for r in recetas}
    fijas = [por_id[e["recipeId"]] for e in aprobadas if e.get("recipeId") in por_id]
    del_congelador = coloca_congelador(libres, disponible(m, c, lunes, hoy), por_id, rng)
    resto = [i for i in range(len(libres)) if i not in del_congelador]
    # Lo del congelador cuenta para las reglas y no se vuelve a sortear esa semana.
    fijas += list(del_congelador.values())
    recientes = _recientes(m, lunes) | {r["id"] for r in del_congelador.values()}
    sorteo = elige(recetas, [libres[i][1] for i in resto], fijas, recientes, rng)
    elegidas: dict[int, tuple[dict, bool]] = {i: (r, True) for i, r in del_congelador.items()}
    elegidas.update({i: (r, False) for i, r in zip(resto, sorteo) if r is not None})
    return [
        db.mete_borrador(c, libres[i][0], libres[i][1], r["id"], congelador=cong)
        for i, (r, cong) in sorted(elegidas.items())
    ]


def cambia(m: Mealie, c: sqlite3.Connection, borrador_id: int, rng: random.Random | None = None) -> dict | None:
    """Sortea otra receta para un plato del borrador, respetando el resto de la semana
    (lo aprobado en Mealie y el resto del borrador). Si era una ración del congelador,
    esa ración se queda congelada para otra semana."""
    rng = rng or random.Random()
    fila = db.borrador_uno(c, borrador_id)
    if fila is None:
        return None
    fecha = dt.date.fromisoformat(fila["fecha"])
    lunes = fecha - dt.timedelta(days=fecha.weekday())
    recetas = m.recetas()
    por_id = {r["id"]: r for r in recetas}
    ids = [e.get("recipeId") for e in m.plan(*_semana(lunes))]
    ids += [b["receta_id"] for b in db.borrador(c, *_semana(lunes)) if b["id"] != borrador_id]
    resto = [por_id[i] for i in ids if i in por_id]
    # La receta actual cuenta como "reciente" para que no vuelva a salir.
    recientes = _recientes(m, lunes) | {fila["receta_id"]}
    nueva = elige(recetas, [fila["tipo"]], resto, recientes, rng)[0]
    if nueva is not None:
        db.cambia_borrador(c, borrador_id, nueva["id"])
        fila.update(receta_id=nueva["id"], congelador=0)
    return fila


def aprueba(m: Mealie, c: sqlite3.Connection, hoy: dt.date) -> int:
    """Pasa el borrador a Mealie desde `hoy`. Si entretanto alguien ocupó ese hueco
    en Mealie, gana lo de Mealie. Lo de días pasados se descarta. Las raciones del
    congelador quedan apuntadas como tales (y en Mealie llevan la nota)."""
    db.borra_borrador(c, dt.date.min, hoy - dt.timedelta(days=1))
    filas = db.borrador(c, hoy, dt.date.max)
    if not filas:
        return 0
    hasta = dt.date.fromisoformat(filas[-1]["fecha"])
    ocupados = {(e["date"], e["entryType"]) for e in m.plan(hoy, hasta)}
    n = 0
    for f in filas:
        if (f["fecha"], f["tipo"]) not in ocupados:
            m.crea_entrada(dt.date.fromisoformat(f["fecha"]), f["tipo"], f["receta_id"],
                           "Del congelador" if f["congelador"] else "")
            if f["congelador"]:
                db.marca_del_congelador(c, f["fecha"], f["tipo"], f["receta_id"])
            n += 1
        db.borra_borrador_id(c, f["id"])
    return n


def descarta(c: sqlite3.Connection) -> int:
    """Borra el borrador entero (incluido lo que quedara de días pasados)."""
    return db.borra_borrador(c, dt.date.min)


def main() -> None:
    ap = argparse.ArgumentParser(description="Genera el borrador semanal")
    ap.add_argument("--lunes", type=dt.date.fromisoformat, help="lunes de la semana (por defecto, el próximo)")
    ap.add_argument("--simular", action="store_true", help="solo muestra el sorteo; no guarda nada")
    ap.add_argument("--avisar", action="store_true", help="avisa al móvil de que hay borrador nuevo")
    args = ap.parse_args()
    hoy = dt.date.today()
    lunes = args.lunes or proximo_lunes(hoy)
    with Mealie(os.environ["MEALIE_URL"], os.environ["MEALIE_TOKEN"]) as m:
        recetas = {r["id"]: r for r in m.recetas()}
        if args.simular:
            hs = huecos(lunes, hoy)
            elegidas = elige(list(recetas.values()), [t for _, t in hs], [], _recientes(m, lunes), random.Random())
            filas = [{"fecha": f.isoformat(), "tipo": t, "receta_id": r["id"] if r else None}
                     for (f, t), r in zip(hs, elegidas)]
        else:
            with db.conexion() as c:
                filas = genera(m, c, lunes, hoy)
            print(f"Borrador semana {lunes}: {len(filas)} platos")
            if args.avisar:
                avisos.avisa_borrador(len(filas), lunes)
    for f in filas:
        r = recetas.get(f["receta_id"]) or {}
        cats = ",".join(x["slug"] for x in r.get("recipeCategory") or [])
        print(f"  {f['fecha']} {f['tipo']:<6} {r.get('name', '—')}  ({cats}) [{', '.join(sorted(_slugs(r)))}]")


if __name__ == "__main__":
    main()
