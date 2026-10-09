"""Tests del borrador semanal contra un Mealie simulado en memoria."""

from __future__ import annotations

import datetime as dt
import json
import random
from collections import Counter

import httpx
import pytest
from fastapi.testclient import TestClient

from panel import app as app_mod
from panel import config, db, generar
from panel.mealie import Mealie

LUNES = dt.date(2026, 10, 12)
DOMINGO_ANTES = dt.date(2026, 10, 11)


def _receta(i: int, *tags: str, cats=("comida",), rating=None, raciones=1) -> dict:
    return {"id": f"r{i}", "slug": f"receta-{i}", "name": f"Receta {i}", "rating": rating, "recipeServings": raciones,
            "tags": [{"slug": t, "name": t} for t in tags],
            "recipeCategory": [{"slug": c, "name": c} for c in cats]}


def _recetario() -> list[dict]:
    """Como el real: legumbres y pasta casi siempre de comida, huevo de cena, pescado de ambas."""
    rs = [_receta(i, "pescado", cats=("comida",) if i < 2 else ("cena",)) for i in range(4)]
    rs += [_receta(10 + i, "legumbres", cats=("comida",) if i < 3 else ("cena",)) for i in range(4)]
    rs += [_receta(20 + i, "pasta") for i in range(5)]
    rs += [_receta(30 + i, "huevo", "rapida", cats=("cena",)) for i in range(6)]
    rs += [_receta(40 + i, cats=("comida",) if i % 2 == 0 else ("cena",)) for i in range(5)]
    rs += [_receta(45, cats=("comida", "cena")), _receta(46, cats=())]
    return rs


TIPOS = ["lunch", "dinner"] * 4 + ["lunch"] + ["lunch", "dinner"]   # los 11 huecos de una semana


class PlanFake:
    def __init__(self, recetas: list[dict]):
        self.recetas = {r["id"]: r for r in recetas}
        self.plan: dict[int, dict] = {}
        self.comentarios: list[dict] = []
        self._sig = 1

    def mete(self, fecha: dt.date, tipo: str, receta_id: str, texto: str = "") -> int:
        id_ = self._sig
        self._sig += 1
        self.plan[id_] = {"id": id_, "date": fecha.isoformat(), "entryType": tipo, "title": "", "text": texto,
                          "recipeId": receta_id, "recipe": self.recetas[receta_id], "groupId": "g", "userId": "u"}
        return id_

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if p == "/api/users/self/ratings":
            return httpx.Response(200, json={"ratings": []})
        if p == "/api/comments":
            return httpx.Response(200, json={"items": self.comentarios, "total_pages": 1})
        if p == "/api/recipes":
            return httpx.Response(200, json={"items": list(self.recetas.values()), "total_pages": 1})
        if p == "/api/households/mealplans" and m == "GET":
            a, b = req.url.params["start_date"], req.url.params["end_date"]
            es = sorted((e for e in self.plan.values() if a <= e["date"] <= b), key=lambda e: e["date"])
            return httpx.Response(200, json={"items": es, "total_pages": 1})
        if p == "/api/households/mealplans" and m == "POST":
            c = json.loads(req.content)
            id_ = self.mete(dt.date.fromisoformat(c["date"]), c["entryType"], c["recipeId"], c.get("text", ""))
            return httpx.Response(201, json=self.plan[id_])
        id_ = int(p.rsplit("/", 1)[1])
        if m == "DELETE":
            del self.plan[id_]
            return httpx.Response(200, json={})
        if m == "PUT":
            c = json.loads(req.content)
            self.plan[id_].update(date=c["date"], text=c["text"], recipeId=c["recipeId"],
                                  recipe=self.recetas[c["recipeId"]])
        return httpx.Response(200, json=self.plan[id_])


@pytest.fixture
def fake():
    return PlanFake(_recetario())


@pytest.fixture
def m(fake):
    return Mealie("http://mealie", "t", transport=httpx.MockTransport(fake))


def _tags(fake: PlanFake, entradas) -> Counter:
    return Counter(t["slug"] for e in entradas for t in fake.recetas[e["recipeId"]]["tags"])


def test_huecos_de_la_semana():
    hs = generar.huecos(LUNES, DOMINGO_ANTES)
    assert len(hs) == 11                                    # L-J ×2, V comida, D ×2
    assert (LUNES + dt.timedelta(days=4), "dinner") not in hs   # viernes sin cena
    assert not any(f == LUNES + dt.timedelta(days=5) for f, _ in hs)   # sábado libre
    # Generando un miércoles para la semana en curso: solo desde ese día.
    assert len(generar.huecos(LUNES, LUNES + dt.timedelta(days=2))) == 7


def test_proximo_lunes():
    assert generar.proximo_lunes(DOMINGO_ANTES) == LUNES
    assert generar.proximo_lunes(LUNES) == LUNES + dt.timedelta(days=7)


@pytest.mark.parametrize("semilla", range(20))
def test_elige_cumple_reglas(semilla):
    elegidas = generar.elige(_recetario(), TIPOS, [], set(), random.Random(semilla))
    assert None not in elegidas and len({r["id"] for r in elegidas}) == 11
    assert all(generar.sirve_para(r, t) for r, t in zip(elegidas, TIPOS))
    c = Counter(t["slug"] for r in elegidas for t in r["tags"])
    assert all(c[k] >= v for k, v in config.MINIMOS.items())
    assert all(c[k] <= v for k, v in config.MAXIMOS.items())


def test_sirve_para_segun_categoria():
    assert generar.sirve_para(_receta(1, cats=("comida",)), "lunch")
    assert not generar.sirve_para(_receta(1, cats=("comida",)), "dinner")
    assert generar.sirve_para(_receta(1, cats=("comida", "cena")), "dinner")
    assert generar.sirve_para(_receta(1, cats=()), "dinner")


def test_elige_deja_hueco_antes_que_cambiar_categoria():
    solo_comidas = [_receta(i) for i in range(3)]
    assert generar.elige(solo_comidas, ["dinner", "lunch"], [], set(), random.Random(0))[0] is None


def test_elige_evita_recientes_si_puede():
    recientes = {f"r{40 + i}" for i in range(6)}
    elegidas = generar.elige(_recetario(), TIPOS, [], recientes, random.Random(1))
    assert not recientes & {r["id"] for r in elegidas}


def _lentejas(raciones=4):
    return _receta(60, "legumbres", "congelable", cats=("comida",), raciones=raciones)


@pytest.mark.parametrize("semilla", range(20))
def test_coloca_congelador_reparte_y_respeta_categoria(semilla):
    hs = generar.huecos(LUNES, DOMINGO_ANTES)
    pisto = _receta(61, "congelable", cats=("cena",))
    out = generar.coloca_congelador(hs, {"r60": 3, "r61": 1}, {"r60": _lentejas(), "r61": pisto}, random.Random(semilla))
    lentejas = [i for i, r in out.items() if r["id"] == "r60"]
    assert len(lentejas) == 3 and all(hs[i][1] == "lunch" for i in lentejas)
    assert len({hs[i][0] for i in lentejas}) == 3                     # tres días distintos
    assert [hs[i][1] for i, r in out.items() if r["id"] == "r61"] == ["dinner"]


def test_coloca_congelador_lo_que_no_cabe_espera():
    hs = [(LUNES, "lunch"), (LUNES, "dinner")]
    out = generar.coloca_congelador(hs, {"r60": 3}, {"r60": _lentejas()}, random.Random(0))
    assert len(out) == 1   # solo hay una comida libre; las otras 2 raciones siguen congeladas


def test_sincroniza_mete_y_saca(fake, m):
    fake.recetas["r60"] = _lentejas()
    fake.mete(LUNES - dt.timedelta(days=6), "lunch", "r60")          # cocinadas: entran 3
    fake.mete(LUNES - dt.timedelta(days=2), "lunch", "r60")          # una ración del congelador: sale 1
    fake.mete(LUNES - dt.timedelta(days=3), "dinner", "r0")          # normal: nada
    fake.mete(LUNES, "lunch", "r60")                                 # aún no ha pasado: nada
    with db.conexion() as c:
        db.marca_del_congelador(c, (LUNES - dt.timedelta(days=2)).isoformat(), "lunch", "r60")
        generar.sincroniza(m, c, LUNES)
        assert db.congelador(c) == {"r60": 2}
        generar.sincroniza(m, c, LUNES)                              # no cuenta dos veces
        assert db.congelador(c) == {"r60": 2}


def test_disponible_cuenta_lo_que_se_va_a_cocinar_y_comer(fake, m):
    fake.recetas["r60"] = _lentejas()
    jueves = LUNES - dt.timedelta(days=4)
    fake.mete(jueves, "lunch", "r60")                                # se cocina antes del lunes: +3
    fake.mete(LUNES + dt.timedelta(days=1), "lunch", "r60")          # ya planificada del congelador: -1
    with db.conexion() as c:
        db.marca_del_congelador(c, (LUNES + dt.timedelta(days=1)).isoformat(), "lunch", "r60")
        db.suma_congelador(c, "r0", 1)
        assert generar.disponible(m, c, LUNES, jueves) == {"r60": 2, "r0": 1}


def test_genera_saca_primero_el_congelador(fake, m):
    fake.recetas["r60"] = _lentejas()
    with db.conexion() as c:
        db.suma_congelador(c, "r60", 3)
        filas = generar.genera(m, c, LUNES, DOMINGO_ANTES, random.Random(4))
        del_cong = [f for f in filas if f["congelador"]]
        assert len(filas) == 11 and [f["receta_id"] for f in del_cong] == ["r60"] * 3
        assert all(f["tipo"] == "lunch" for f in del_cong)
        assert "r60" not in [f["receta_id"] for f in filas if not f["congelador"]]   # no se vuelve a sortear
        # Al aprobar, en Mealie llevan la nota y quedan apuntadas como del congelador.
        generar.aprueba(m, c, DOMINGO_ANTES)
        assert sorted(e["text"] for e in fake.plan.values() if e["recipeId"] == "r60") == ["Del congelador"] * 3
        assert len(db.del_congelador(c, LUNES, LUNES + dt.timedelta(days=6))) == 3
        assert db.congelador(c) == {"r60": 3}   # salen del inventario cuando pasa el día, no al aprobar


def test_cambiar_una_racion_del_congelador(fake, m):
    fake.recetas["r60"] = _lentejas()
    with db.conexion() as c:
        f = db.mete_borrador(c, LUNES, "lunch", "r60", congelador=True)
        nueva = generar.cambia(m, c, f["id"], random.Random(1))
        assert nueva["receta_id"] != "r60" and nueva["congelador"] == 0
        assert db.borrador_uno(c, f["id"])["congelador"] == 0


def test_genera_respeta_aprobadas_y_regenera(fake, m):
    fake.mete(LUNES, "lunch", "r0")   # ya aprobada en Mealie
    with db.conexion() as c:
        creadas = generar.genera(m, c, LUNES, DOMINGO_ANTES, random.Random(3))
        assert len(creadas) == 10
        assert len(fake.plan) == 1                          # el borrador no toca Mealie
        huecos = {(f["fecha"], f["tipo"]) for f in creadas} | {(LUNES.isoformat(), "lunch")}
        assert len(huecos) == 11                            # sin huecos duplicados
        ids = [f["receta_id"] for f in creadas] + ["r0"]
        assert len(set(ids)) == 11                          # sin recetas repetidas
        assert all(generar.sirve_para(fake.recetas[f["receta_id"]], f["tipo"]) for f in creadas)
        assert Counter(t["slug"] for i in ids for t in fake.recetas[i]["tags"])["pescado"] <= config.MAXIMOS["pescado"]
        # Regenerar sustituye el borrador de esa semana, no lo acumula.
        generar.genera(m, c, LUNES, DOMINGO_ANTES, random.Random(4))
        assert len(db.borrador(c, LUNES, LUNES + dt.timedelta(days=6))) == 10


def test_cambia_y_aprueba(fake, m):
    with db.conexion() as c:
        creadas = generar.genera(m, c, LUNES, DOMINGO_ANTES, random.Random(5))
        f = creadas[0]
        nueva = generar.cambia(m, c, f["id"], random.Random(6))["receta_id"]
        assert nueva != f["receta_id"] and generar.sirve_para(fake.recetas[nueva], f["tipo"])
        assert len({b["receta_id"] for b in db.borrador(c, LUNES, LUNES + dt.timedelta(days=6))}) == 11
        # Alguien ocupa un hueco en Mealie antes de aprobar: gana Mealie.
        otra = next(b for b in creadas if b["id"] != f["id"])
        fake.mete(dt.date.fromisoformat(otra["fecha"]), otra["tipo"], "r46")
        assert generar.aprueba(m, c, DOMINGO_ANTES) == 10
        assert db.borrador(c, dt.date.min, dt.date.max) == []
        assert len(fake.plan) == 11 and all(e["text"] == "" for e in fake.plan.values())


def test_aprueba_descarta_lo_de_dias_pasados(fake, m):
    with db.conexion() as c:
        generar.genera(m, c, LUNES, DOMINGO_ANTES, random.Random(7))
        # Se aprueba el miércoles: lunes y martes del borrador ya pasaron.
        assert generar.aprueba(m, c, LUNES + dt.timedelta(days=2)) == 7
        assert db.borrador(c, dt.date.min, dt.date.max) == []


def test_endpoints_borrador(fake, monkeypatch):
    monkeypatch.setattr(app_mod, "_cliente", lambda: Mealie("http://mealie", "t", transport=httpx.MockTransport(fake)))
    monkeypatch.setattr(app_mod, "_hoy", lambda: DOMINGO_ANTES)
    cli = TestClient(app_mod.app)
    assert cli.post("/api/borrador/generar", json={"lunes": "2026-10-13"}).status_code == 422
    assert cli.post("/api/borrador/generar", json={}).json() == {"lunes": "2026-10-12", "creadas": 11}
    semana = cli.get("/api/plan").json()["semana"]
    assert len(semana) == 11 and all(e["borrador"] and e["titulo"] for e in semana)
    # Las notas de la receta aparecen en el plato del borrador.
    fake.comentarios.append({"recipeId": semana[0]["receta_id"], "text": "con menos sal",
                             "createdAt": "2026-10-03T21:00:00", "user": {"username": "usuario"}})
    semana = cli.get("/api/plan").json()["semana"]
    assert semana[0]["notas_receta"] == [{"texto": "con menos sal", "fecha": "2026-10-03", "autor": "usuario"}]
    assert all(e["notas_receta"] == [] for e in semana[1:])
    assert cli.post(f"/api/borrador/{semana[0]['id']}/cambiar").status_code == 200
    assert cli.post("/api/borrador/999/cambiar").status_code == 404
    assert cli.post("/api/borrador/aprobar").json() == {"aprobadas": 11}
    assert not any(e["borrador"] for e in cli.get("/api/plan").json()["semana"])
    cli.post("/api/borrador/generar", json={"lunes": "2026-10-19"})
    assert cli.delete("/api/borrador").json() == {"descartadas": 11}
    assert len(fake.plan) == 11
