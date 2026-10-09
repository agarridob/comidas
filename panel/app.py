"""API + panel web de comidas (capa fina sobre Mealie y HA).

El navegador nunca ve los tokens: todo pasa por aquí. El estado propio del panel
(borrador, descongelado) vive en SQLite (panel/db.py).
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config, db, generar, ingredientes
from .ha import HA
from .mealie import TIPOS_PANEL, Mealie, a_posponer, tiene_etiqueta

app = FastAPI(title="comidas", docs_url="/api/docs")
_ESTATICO = Path(__file__).with_name("static")
_TZ = ZoneInfo(os.environ.get("TZ", "Europe/Madrid"))
# Días de plan que se cargan hacia delante: posponer corre toda la secuencia y la
# pestaña Semana los muestra.
_HORIZONTE = 14
# Días hacia atrás en los que un plato sin valorar sale en "Puntúa".
_PUNTUA_DIAS = 7


def _cliente() -> Mealie:
    return Mealie(os.environ["MEALIE_URL"], os.environ["MEALIE_TOKEN"])


def _hoy() -> dt.date:
    return dt.datetime.now(_TZ).date()


def _etiqueta_congelable() -> str:
    return os.environ.get("ETIQUETA_CONGELABLE", config.ETIQUETA_CONGELABLE)


def _publica() -> str:
    return (os.environ.get("MEALIE_PUBLIC_URL") or os.environ.get("MEALIE_URL", "")).rstrip("/")


def _url_receta(slug: str) -> str:
    return f"{_publica()}/g/{os.environ.get('MEALIE_GROUP_SLUG', 'home')}/r/{slug}"


def _url_foto(receta: dict) -> str | None:
    """Foto de la receta (los medios de Mealie son públicos; la carga el navegador)."""
    if not receta.get("image"):
        return None
    return f"{_publica()}/api/media/recipes/{receta['id']}/images/min-original.webp?v={receta['image']}"


_UNIDADES = ((r"\bhours?\b|\bhrs?\b", "h"), (r"\bminutes?\b|\bmins?\b", "min"))


def _tiempo(texto: str | None) -> str | None:
    """Mealie guarda el tiempo como texto libre y en inglés ("1 hour 20 minutes")."""
    if not texto:
        return None
    for patron, unidad in _UNIDADES:
        texto = re.sub(patron, unidad, texto, flags=re.IGNORECASE)
    return texto


def _entrada(e: dict, valoraciones: dict[str, float], *, borrador: bool = False) -> dict:
    """Entrada del plan (de Mealie o del borrador) reducida a lo que pinta el panel."""
    r = e.get("recipe") or {}
    return {
        "id": e["id"],   # id de Mealie, o id del borrador si borrador=True
        "fecha": e["date"],
        "tipo": e["entryType"],
        "titulo": r.get("name") or e.get("title") or "",
        "nota": e.get("text") or "",
        "borrador": borrador,
        "slug": r.get("slug"),
        "receta_id": r.get("id"),
        "url": _url_receta(r["slug"]) if r.get("slug") else None,
        "foto": _url_foto(r) if r else None,
        "tiempo": _tiempo(r.get("totalTime")),
        "congelable": tiene_etiqueta(e, _etiqueta_congelable()),
        "raciones": generar.raciones(r) if r else None,
        "estrellas": valoraciones.get(r.get("id")) if r else None,
    }


def _nota(c: dict) -> dict:
    """Comentario de Mealie reducido: las notas de "cómo salió" de una receta."""
    return {"texto": c.get("text") or "", "fecha": (c.get("createdAt") or "")[:10],
            "autor": (c.get("user") or {}).get("username")}


def _como_entrada(fila: dict, recetas: dict[str, dict]) -> dict:
    """Fila del borrador con la forma de una entrada de Mealie."""
    return {"id": fila["id"], "date": fila["fecha"], "entryType": fila["tipo"], "text": "",
            "recipe": recetas.get(fila["receta_id"])}


def _por_valorar(entradas: list[dict], desde: dt.date, hoy: dt.date) -> list[dict]:
    """Platos comidos entre `desde` y ayer cuya receta sigue sin valorar, del más
    reciente al más antiguo y una vez por receta (la valoración es de la receta).
    Las raciones del congelador no cuentan: son la segunda vuelta de algo ya hecho."""
    vistas: set[str] = set()
    fuera = []
    for e in sorted(entradas, key=lambda e: (e["fecha"], e["tipo"] == "dinner"), reverse=True):
        if not (desde.isoformat() <= e["fecha"] < hoy.isoformat()) or not e["slug"]:
            continue
        if e["estrellas"] or e["del_congelador"] or e["borrador"] or e["receta_id"] in vistas:
            continue
        vistas.add(e["receta_id"])
        fuera.append(e)
    return fuera


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


@app.get("/api/plan")
def plan() -> JSONResponse:
    """Hoy, mañana (para descongelar), lo de la última semana por valorar, la semana y el congelador.
    El borrador pendiente se mezcla con el plan de Mealie, marcado como tal. Las
    raciones del congelador llevan `del_congelador`."""
    hoy = _hoy()
    desde, manana = hoy - dt.timedelta(days=_PUNTUA_DIAS), hoy + dt.timedelta(days=1)
    hasta = hoy + dt.timedelta(days=_HORIZONTE)
    with _cliente() as m:
        with db.conexion() as c:
            generar.sincroniza(m, c, hoy)
            filas = db.borrador(c, hoy, hasta)
            frigo = db.descongelados(c, manana)
            del_cong = db.del_congelador(c, desde, hasta)
            stock = db.congelador(c)
        crudo = [e for e in m.plan(desde, hasta) if e["entryType"] in TIPOS_PANEL]
        valoraciones = m.mis_valoraciones()
        recetas = {r["id"]: r for r in m.recetas()} if (filas or stock) else {}
        # Las notas de cada receta se enseñan en el borrador, para decidir al revisarlo.
        comentarios = m.comentarios() if filas else []
    notas: dict[str, list[dict]] = {}
    for c in comentarios:
        notas.setdefault(c.get("recipeId"), []).append(_nota(c))
    entradas = [{**_entrada(e, valoraciones), "del_congelador": (e["date"], e["entryType"]) in del_cong}
                for e in crudo]
    entradas += [{**_entrada(_como_entrada(f, recetas), valoraciones, borrador=True),
                  "del_congelador": bool(f["congelador"]), "notas_receta": notas.get(f["receta_id"], [])}
                 for f in filas]
    entradas.sort(key=lambda e: (e["fecha"], e["tipo"] != "lunch"))
    del_dia = lambda d: [e for e in entradas if e["fecha"] == d.isoformat()]  # noqa: E731
    return JSONResponse({
        "hoy": hoy.isoformat(),
        "comidas_hoy": del_dia(hoy),
        "por_valorar": _por_valorar(entradas, desde, hoy),
        "descongelar": [{**e, "descongelado": e["tipo"] in frigo} for e in del_dia(manana) if e["del_congelador"]],
        # Desde el lunes de esta semana: lo ya comido se ve (atenuado) en el Plan.
        "semana": [e for e in entradas if e["fecha"] >= (hoy - dt.timedelta(days=hoy.weekday())).isoformat()],
        "congelador": sorted(
            ({"receta_id": rid, "raciones": n, "nombre": (recetas.get(rid) or {}).get("name", "¿receta borrada?"),
              "slug": (recetas.get(rid) or {}).get("slug")} for rid, n in stock.items()),
            key=lambda x: x["nombre"]),
    })


class Ajuste(BaseModel):
    delta: int = Field(ge=-20, le=20)


@app.post("/api/congelador/{receta_id}")
def ajusta_congelador(receta_id: str, a: Ajuste) -> dict:
    """Corrección a mano del inventario (se tiró algo, se congeló fuera del plan…)."""
    with db.conexion() as c:
        return {"raciones": db.suma_congelador(c, receta_id, a.delta)}


class Posponer(BaseModel):
    dias: int = Field(1, ge=-7, le=7)


@app.post("/api/plan/{entrada_id}/posponer")
def posponer(entrada_id: int, p: Posponer) -> dict:
    """Corre `dias` la entrada de Mealie y todas las posteriores del mismo tipo (y con
    ellas el borrador y lo descongelado). Devuelve los ids movidos; deshacer = la
    misma llamada con días en negativo sobre la misma entrada."""
    with _cliente() as m:
        entrada = m.entrada(entrada_id)
        desde = dt.date.fromisoformat(entrada["date"])
        mover = a_posponer(m.plan(desde, desde + dt.timedelta(days=60)), entrada)
        # Al adelantar, de la más antigua a la más nueva; al retrasar, al revés: así
        # nunca coinciden dos entradas del mismo tipo el mismo día a mitad de camino.
        mover.sort(key=lambda e: e["date"], reverse=p.dias > 0)
        for e in mover:
            m.mueve_entrada(e, p.dias)
    with db.conexion() as c:
        db.desplaza(c, entrada["entryType"], desde, p.dias)
    return {"movidas": [e["id"] for e in mover]}


class Descongelado(BaseModel):
    fecha: dt.date
    tipo: str = Field(pattern="^(lunch|dinner)$")
    hecho: bool = True


@app.post("/api/descongelado")
def descongelado(d: Descongelado) -> dict:
    """Lo de esa comida ya está (o ya no está) en el frigo."""
    with db.conexion() as c:
        db.marca_descongelado(c, d.fecha, d.tipo, d.hecho)
    return {"ok": True}


class Generar(BaseModel):
    lunes: dt.date | None = None   # por defecto, el próximo lunes


@app.post("/api/borrador/generar")
def genera_borrador(g: Generar) -> dict:
    """Genera (o regenera) el borrador de una semana. Lo mismo que hace el timer del
    domingo; lo ya aprobado en Mealie se respeta."""
    hoy = _hoy()
    lunes = g.lunes or generar.proximo_lunes(hoy)
    if lunes.weekday() != 0:
        raise HTTPException(422, "lunes debe ser un lunes")
    with _cliente() as m, db.conexion() as c:
        creadas = generar.genera(m, c, lunes, hoy)
    return {"lunes": lunes.isoformat(), "creadas": len(creadas)}


@app.post("/api/borrador/{borrador_id}/cambiar")
def cambia_borrador(borrador_id: int) -> dict:
    with _cliente() as m, db.conexion() as c:
        if generar.cambia(m, c, borrador_id) is None:
            raise HTTPException(404, "Ese plato ya no está en el borrador")
    return {"ok": True}


@app.post("/api/borrador/aprobar")
def aprueba_borrador() -> dict:
    with _cliente() as m, db.conexion() as c:
        n = generar.aprueba(m, c, _hoy())
    return {"aprobadas": n}


@app.delete("/api/borrador")
def descarta_borrador() -> dict:
    with db.conexion() as c:
        n = generar.descarta(c)
    return {"descartadas": n}


@app.get("/api/recetas/{slug}")
def receta(slug: str) -> dict:
    """Receta para cocinar desde el panel: ingredientes y pasos tal cual están en Mealie."""
    with _cliente() as m:
        r = m.receta(slug)
        comentarios = m.comentarios_receta(slug)
    return {
        "comentarios": [_nota(c) for c in sorted(comentarios, key=lambda c: c.get("createdAt") or "", reverse=True)],
        "nombre": r.get("name"),
        "url": _url_receta(slug),
        "raciones": r.get("recipeServings") or None,
        "preparacion": _tiempo(r.get("prepTime")),
        "coccion": _tiempo(r.get("performTime") or r.get("cookTime")),
        "total": _tiempo(r.get("totalTime")),
        # Una línea con `titulo` y sin texto es una cabecera de sección ("Para la salsa").
        "ingredientes": [
            {"titulo": i.get("title") or None,
             "texto": (i.get("display") or i.get("note") or "").strip()}
            for i in r.get("recipeIngredient") or []
        ],
        "pasos": [
            {"titulo": p.get("title") or None, "texto": (p.get("text") or "").strip()}
            for p in r.get("recipeInstructions") or []
            if (p.get("text") or "").strip()
        ],
        "notas": [{"titulo": n.get("title") or None, "texto": n.get("text") or ""} for n in r.get("notes") or []
                  if n.get("text")],
    }


class Nota(BaseModel):
    texto: str = Field(min_length=1, max_length=500)


@app.post("/api/recetas/{slug}/nota")
def anota(slug: str, n: Nota) -> dict:
    """Nota de "cómo salió" como comentario de la receta en Mealie."""
    with _cliente() as m:
        c = m.comenta(m.receta(slug)["id"], n.texto.strip())
    return _nota(c)


class Valoracion(BaseModel):
    estrellas: float = Field(ge=0, le=5)


@app.post("/api/recetas/{slug}/valoracion")
def valora(slug: str, v: Valoracion) -> dict:
    with _cliente() as m:
        m.valora(m.yo()["id"], slug, v.estrellas)
    return {"ok": True}


def _ha() -> HA:
    return HA(
        os.environ["HA_URL"],
        os.environ["HA_TOKEN"],
        os.environ.get("HA_TODO_ENTITY", "todo.lista_de_la_compra"),
    )


@app.get("/api/compra")
def compra() -> JSONResponse:
    with _ha() as h:
        items = h.items()
    return JSONResponse({
        "items": [
            {"id": i["uid"], "texto": i.get("summary") or "", "hecho": i.get("status") == "completed"}
            for i in items
        ],
    })


class Marca(BaseModel):
    hecho: bool


@app.put("/api/compra/{uid}")
def marca(uid: str, b: Marca) -> dict:
    with _ha() as h:
        h.marca(uid, b.hecho)
    return {"ok": True}


class Nuevo(BaseModel):
    texto: str = Field(min_length=1, max_length=200)


@app.post("/api/compra")
def anade(n: Nuevo) -> dict:
    with _ha() as h:
        h.anade(n.texto.strip())
    return {"ok": True}


def _platos_compra(m: Mealie, c, hoy: dt.date) -> tuple[list[dict], int]:
    """Platos aprobados de los próximos días cuyos ingredientes aún no se pasaron a
    la compra, y cuántos ya se pasaron."""
    hasta = hoy + dt.timedelta(days=config.DIAS_COMPRA - 1)
    hechos = db.compra_hecha(c, hoy, hasta)
    # Lo que sale del congelador no se compra.
    del_cong = db.del_congelador(c, hoy, hasta)
    platos = [e for e in m.plan(hoy, hasta) if e["entryType"] in TIPOS_PANEL and e.get("recipe")
              and (e["date"], e["entryType"]) not in del_cong]
    pendientes = [e for e in platos if (e["date"], e["entryType"], e["recipeId"]) not in hechos]
    return pendientes, len(platos) - len(pendientes)


@app.get("/api/compra/plan")
def compra_del_plan() -> JSONResponse:
    """Propuesta de compra con los ingredientes del plan aprobado (vista previa: no
    añade nada). Básicos, opcionales y lo ya pendiente en la lista salen desmarcados."""
    hoy = _hoy()
    with _cliente() as m, db.conexion() as c:
        platos, ya_hechos = _platos_compra(m, c, hoy)
        recetas = {}
        for e in platos:
            slug = e["recipe"]["slug"]
            if slug not in recetas:
                recetas[slug] = m.receta(slug)
    with _ha() as h:
        en_lista = {ingredientes.clave(i.get("summary") or "") for i in h.items() if i.get("status") != "completed"}
    items = ingredientes.agrega([recetas[e["recipe"]["slug"]] for e in platos])
    return JSONResponse({
        "desde": hoy.isoformat(),
        "hasta": (hoy + dt.timedelta(days=config.DIAS_COMPRA - 1)).isoformat(),
        "ya_hechos": ya_hechos,
        "platos": [{"fecha": e["date"], "tipo": e["entryType"], "receta_id": e["recipeId"],
                    "titulo": e["recipe"]["name"]} for e in platos],
        "items": [
            {
                "texto": it.texto(), "nombre": it.nombre, "cantidad": it.cantidad(), "recetas": it.recetas,
                "basico": it.basico, "opcional": it.opcional, "en_lista": it.clave in en_lista,
                "marcado": not (it.basico or it.opcional or it.clave in en_lista),
            }
            for it in items
        ],
    })


class PasarCompra(BaseModel):
    textos: list[str] = Field(max_length=200)
    platos: list[tuple[dt.date, str, str]]   # (fecha, tipo, receta_id) de la vista previa


@app.post("/api/compra/plan")
def pasa_a_compra(p: PasarCompra) -> dict:
    """Añade a la lista de HA lo marcado y apunta esos platos como ya pasados."""
    with _ha() as h:
        for t in p.textos:
            if t.strip():
                h.anade(t.strip())
    with db.conexion() as c:
        db.marca_compra_hecha(c, [(f.isoformat(), t, r) for f, t, r in p.platos])
    return {"anadidos": len([t for t in p.textos if t.strip()]), "platos": len(p.platos)}


@app.delete("/api/compra/comprados")
def vacia_comprados() -> dict:
    with _ha() as h:
        h.vacia_completados()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=_ESTATICO), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_ESTATICO / "index.html", headers={"Cache-Control": "no-cache"})
