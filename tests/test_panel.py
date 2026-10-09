"""Tests del panel contra Mealie y HA simulados (httpx.MockTransport): sin red."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from panel import app as app_mod
from panel import db
from panel.ha import HA
from panel.mealie import Mealie, a_posponer, tiene_etiqueta

HOY = dt.date(2026, 10, 5)


def _d(n: int) -> str:
    return (HOY + dt.timedelta(days=n)).isoformat()


def _receta(id_: str, nombre: str, tags=()) -> dict:
    return {"id": id_, "slug": nombre.lower().replace(" ", "-"), "name": nombre, "image": "abc",
            "totalTime": "30 min", "tags": [{"name": t, "slug": t} for t in tags]}


def _entrada(id_: int, dias: int, tipo: str, receta: dict | None) -> dict:
    return {"id": id_, "date": _d(dias), "entryType": tipo, "title": "", "text": "",
            "recipeId": receta["id"] if receta else None, "recipe": receta,
            "groupId": "g", "userId": "u", "householdId": "h"}


class FakeMealie:
    """Estado mínimo de Mealie: plan, valoraciones y una lista de la compra."""

    def __init__(self):
        lentejas = _receta("r1", "Lentejas")
        pollo = {**_receta("r2", "Pollo al curry", tags=["congelable"]), "recipeServings": 2}
        tortilla = _receta("r3", "Tortilla")
        self.plan = {
            1: _entrada(1, -1, "dinner", tortilla),
            2: _entrada(2, 0, "lunch", lentejas),
            3: _entrada(3, 0, "dinner", tortilla),
            4: _entrada(4, 1, "dinner", pollo),
            5: _entrada(5, 1, "lunch", lentejas),
            6: _entrada(6, 2, "breakfast", None),
        }
        self.ratings = {"r3": 4.0}
        self.comentarios: list[dict] = []
        self.ingredientes = {
            "lentejas": [{"note": "300 g de lentejas"}, {"note": "1 cebolla"}, {"note": "Sal"}],
            "tortilla": [{"note": "4 huevos"}, {"note": "1 cebolla"}, {"note": "Aceite de oliva"}],
            "pollo-al-curry": [{"note": "2 pechugas de pollo"}, {"note": "Leche"}],
        }
        self.valorado = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if p == "/api/households/mealplans" and m == "GET":
            a, b = req.url.params["start_date"], req.url.params["end_date"]
            es = sorted((e for e in self.plan.values() if a <= e["date"] <= b), key=lambda e: e["date"])
            return httpx.Response(200, json={"items": es, "total_pages": 1})
        if p.startswith("/api/households/mealplans/"):
            id_ = int(p.rsplit("/", 1)[1])
            if m == "PUT":
                cuerpo = json.loads(req.content)
                assert cuerpo["id"] == id_ and cuerpo["groupId"] and cuerpo["userId"]
                self.plan[id_] = {**self.plan[id_], "date": cuerpo["date"]}
            return httpx.Response(200, json=self.plan[id_])
        if p == "/api/users/self/ratings":
            return httpx.Response(200, json={"ratings": [{"recipeId": k, "rating": v} for k, v in self.ratings.items()]})
        if p == "/api/comments" and m == "POST":
            c = json.loads(req.content)
            nuevo = {"id": f"c{len(self.comentarios)}", "recipeId": c["recipeId"], "text": c["text"],
                     "createdAt": f"2026-10-0{len(self.comentarios) + 1}T20:00:00", "user": {"username": "usuario"}}
            self.comentarios.append(nuevo)
            return httpx.Response(201, json=nuevo)
        if p.endswith("/comments"):
            slug = p.split("/")[3]
            rid = next(e["recipe"]["id"] for e in self.plan.values() if e["recipe"] and e["recipe"]["slug"] == slug)
            return httpx.Response(200, json=[c for c in self.comentarios if c["recipeId"] == rid])
        if p == "/api/recipes":
            rs = {e["recipe"]["id"]: e["recipe"] for e in self.plan.values() if e["recipe"]}
            return httpx.Response(200, json={"items": list(rs.values()), "total_pages": 1})
        if p.startswith("/api/recipes/"):
            slug = p.rsplit("/", 1)[1]
            r = next(e["recipe"] for e in self.plan.values() if e["recipe"] and e["recipe"]["slug"] == slug)
            return httpx.Response(200, json={
                **r, "recipeIngredient": self.ingredientes.get(slug, []), "recipeServings": 2.0,
                "prepTime": "10 minutes", "notes": [],
                "recipeInstructions": [{"title": "", "text": "Sofríe la cebolla."}, {"title": "", "text": "  "},
                                       {"title": "Final", "text": "Añade las lentejas."}],
            })
        if p == "/api/users/self":
            return httpx.Response(200, json={"id": "u"})
        if p.startswith("/api/users/u/ratings/"):
            self.valorado.append((p.rsplit("/", 1)[1], json.loads(req.content)["rating"]))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"path": p})


class FakeHA:
    """Servicios todo.* de Home Assistant sobre una lista en memoria."""

    ENTIDAD = "todo.lista_de_la_compra"

    def __init__(self):
        self.items = [{"uid": "i1", "summary": "Leche", "status": "needs_action"}]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        datos = json.loads(req.content)
        assert datos["entity_id"] == self.ENTIDAD
        servicio = req.url.path.rsplit("/", 1)[1]
        if servicio == "get_items":
            assert "return_response" in req.url.params
            return httpx.Response(200, json={"changed_states": [], "service_response": {self.ENTIDAD: {"items": self.items}}})
        if servicio == "update_item":
            next(i for i in self.items if i["uid"] == datos["item"])["status"] = datos["status"]
        elif servicio == "add_item":
            self.items.append({"uid": f"i{len(self.items) + 1}", "summary": datos["item"], "status": "needs_action"})
        elif servicio == "remove_completed_items":
            self.items = [i for i in self.items if i["status"] != "completed"]
        return httpx.Response(200, json=[])


@pytest.fixture
def fake(monkeypatch):
    f = FakeMealie()
    monkeypatch.setattr(app_mod, "_cliente", lambda: Mealie("http://mealie", "t", transport=httpx.MockTransport(f)))
    monkeypatch.setattr(app_mod, "_hoy", lambda: HOY)
    return f


@pytest.fixture
def ha(monkeypatch):
    f = FakeHA()
    monkeypatch.setattr(app_mod, "_ha", lambda: HA("http://ha", "t", FakeHA.ENTIDAD, transport=httpx.MockTransport(f)))
    return f


@pytest.fixture
def cli(fake):
    return TestClient(app_mod.app)


def test_tiempo_en_castellano():
    assert app_mod._tiempo("20 minutes") == "20 min"
    assert app_mod._tiempo("1 hour 30 Minutes") == "1 h 30 min"
    assert app_mod._tiempo(None) is None


def test_tiene_etiqueta_por_nombre_o_slug():
    e = _entrada(1, 0, "dinner", _receta("r", "X", tags=["Congelable"]))
    assert tiene_etiqueta(e, "congelable")
    assert not tiene_etiqueta(_entrada(2, 0, "dinner", None), "congelable")


def test_a_posponer_solo_mismo_tipo_y_posteriores():
    plan = list(FakeMealie().plan.values())
    assert [e["id"] for e in a_posponer(plan, plan[2])] == [3, 4]   # cena de hoy y de mañana


def _pollo_del_congelador() -> None:
    """La cena de mañana (pollo, congelable) es una ración del congelador."""
    with db.conexion() as c:
        db.marca_del_congelador(c, _d(1), "dinner", "r2")


def test_plan_hoy_y_descongelar(cli, fake):
    j = cli.get("/api/plan").json()
    assert j["descongelar"] == []                  # el pollo de mañana se cocina mañana: nada que descongelar
    assert [e["raciones"] for e in j["semana"] if e["id"] == 4] == [2]
    _pollo_del_congelador()
    j = cli.get("/api/plan").json()
    assert {e["id"] for e in j["comidas_hoy"]} == {2, 3}
    assert [(e["titulo"], e["del_congelador"]) for e in j["descongelar"]] == [("Pollo al curry", True)]
    assert all(e["tipo"] in ("lunch", "dinner") for e in j["semana"])   # sin desayunos
    assert j["comidas_hoy"][0]["url"].endswith("/g/home/r/lentejas")


def test_por_valorar_ultima_semana(cli, fake):
    lentejas, pollo = fake.plan[2]["recipe"], fake.plan[4]["recipe"]
    fake.plan |= {
        10: _entrada(10, -2, "lunch", lentejas),
        11: _entrada(11, -4, "dinner", lentejas),    # repetida: sale una vez, la más reciente
        12: _entrada(12, -3, "lunch", pollo),
        13: _entrada(13, -8, "dinner", pollo),       # fuera de la ventana de 7 días
    }
    with db.conexion() as c:
        db.marca_del_congelador(c, _d(-3), "lunch", "r2")   # ración del congelador: no se pregunta
    j = cli.get("/api/plan").json()
    # La tortilla de ayer ya tiene 4★; lo de hoy aún no se ha comido.
    assert [(e["id"], e["fecha"]) for e in j["por_valorar"]] == [(10, _d(-2))]
    fake.ratings["r1"] = 0   # 0 en Mealie = sin valorar
    assert [e["id"] for e in cli.get("/api/plan").json()["por_valorar"]] == [10]
    fake.ratings["r1"] = 5
    assert cli.get("/api/plan").json()["por_valorar"] == []


def test_congelador_a_mano(cli, fake):
    assert cli.get("/api/plan").json()["congelador"] == []
    assert cli.post("/api/congelador/r2", json={"delta": 2}).json() == {"raciones": 2}
    assert cli.get("/api/plan").json()["congelador"] == [
        {"receta_id": "r2", "raciones": 2, "nombre": "Pollo al curry", "slug": "pollo-al-curry"}]
    assert cli.post("/api/congelador/r2", json={"delta": -5}).json() == {"raciones": 0}   # nunca negativo
    assert cli.get("/api/plan").json()["congelador"] == []


def test_posponer_y_deshacer(cli, fake):
    r = cli.post("/api/plan/3/posponer", json={"dias": 1}).json()
    assert sorted(r["movidas"]) == [3, 4]
    assert fake.plan[3]["date"] == _d(1) and fake.plan[4]["date"] == _d(2)
    assert fake.plan[2]["date"] == _d(0)   # la comida no se toca
    cli.post("/api/plan/3/posponer", json={"dias": -1})
    assert fake.plan[3]["date"] == _d(0) and fake.plan[4]["date"] == _d(1)


def test_descongelado(cli, fake):
    _pollo_del_congelador()
    assert cli.get("/api/plan").json()["descongelar"][0]["descongelado"] is False
    cli.post("/api/descongelado", json={"fecha": _d(1), "tipo": "dinner", "hecho": True})
    assert cli.get("/api/plan").json()["descongelar"][0]["descongelado"] is True
    cli.post("/api/descongelado", json={"fecha": _d(1), "tipo": "dinner", "hecho": False})
    assert cli.get("/api/plan").json()["descongelar"][0]["descongelado"] is False
    assert cli.post("/api/descongelado", json={"fecha": _d(1), "tipo": "snack"}).status_code == 422


def test_posponer_arrastra_descongelado_y_borrador(cli, fake):
    with db.conexion() as c:
        db.marca_descongelado(c, HOY + dt.timedelta(days=1), "dinner", True)
        db.mete_borrador(c, HOY + dt.timedelta(days=3), "dinner", "r1")
        db.mete_borrador(c, HOY + dt.timedelta(days=3), "lunch", "r1")
        db.marca_del_congelador(c, _d(1), "dinner", "r2")
    cli.post("/api/plan/3/posponer", json={"dias": 1})   # cena de hoy
    with db.conexion() as c:
        assert db.del_congelador(c, HOY, HOY + dt.timedelta(days=9)) == {(_d(2), "dinner")}
        assert db.descongelados(c, HOY + dt.timedelta(days=2)) == {"dinner"}
        assert db.descongelados(c, HOY + dt.timedelta(days=1)) == set()
        assert {(b["fecha"], b["tipo"]) for b in db.borrador(c, HOY, HOY + dt.timedelta(days=9))} == \
            {(_d(4), "dinner"), (_d(3), "lunch")}   # la comida no se mueve
    cli.post("/api/plan/3/posponer", json={"dias": -1})  # deshacer
    with db.conexion() as c:
        assert db.descongelados(c, HOY + dt.timedelta(days=1)) == {"dinner"}
        assert {(b["fecha"], b["tipo"]) for b in db.borrador(c, HOY, HOY + dt.timedelta(days=9))} == \
            {(_d(3), "dinner"), (_d(3), "lunch")}


def test_compra_no_cuenta_lo_del_congelador(cli, fake, ha):
    _pollo_del_congelador()
    p = cli.get("/api/compra/plan").json()
    assert len(p["platos"]) == 3 and "Pollo al curry" not in [x["titulo"] for x in p["platos"]]


def test_compra_del_plan(cli, fake, ha):
    p = cli.get("/api/compra/plan").json()
    # Hoy (comida + cena) y mañana (cena + comida); el desayuno del día 2 no cuenta.
    assert len(p["platos"]) == 4 and p["ya_hechos"] == 0
    items = {i["nombre"].lower(): i for i in p["items"]}
    assert items["cebolla"]["texto"] == "Cebolla (3)"        # lentejas ×2 + tortilla
    assert items["lentejas"]["texto"] == "Lentejas (600 g)"
    assert items["sal"]["basico"] and not items["sal"]["marcado"]
    assert items["leche"]["en_lista"] and not items["leche"]["marcado"]   # ya pendiente en HA
    ha.items.append({"uid": "x", "summary": "Huevos", "status": "needs_action"})
    items = {i["nombre"].lower(): i for i in cli.get("/api/compra/plan").json()["items"]}
    assert items["huevos"]["en_lista"] and not items["huevos"]["marcado"]

    marcados = [i["texto"] for i in items.values() if i["marcado"]]
    platos = [[x["fecha"], x["tipo"], x["receta_id"]] for x in p["platos"]]
    assert cli.post("/api/compra/plan", json={"textos": marcados, "platos": platos}).json() == \
        {"anadidos": len(marcados), "platos": 4}
    assert {i["summary"] for i in ha.items} >= set(marcados)
    p = cli.get("/api/compra/plan").json()
    assert p["platos"] == [] and p["ya_hechos"] == 4           # no se vuelve a proponer


def test_receta(cli, fake):
    r = cli.get("/api/recetas/lentejas").json()
    assert r["nombre"] == "Lentejas" and r["raciones"] == 2.0 and r["preparacion"] == "10 min"
    assert [i["texto"] for i in r["ingredientes"]] == ["300 g de lentejas", "1 cebolla", "Sal"]
    assert r["pasos"] == [{"titulo": None, "texto": "Sofríe la cebolla."},   # el paso vacío se omite
                          {"titulo": "Final", "texto": "Añade las lentejas."}]
    assert r["url"].endswith("/g/home/r/lentejas")


def test_posponer_dos_dias_y_deshacer(cli, fake):
    cli.post("/api/plan/2/posponer", json={"dias": 2})   # comida de hoy
    assert fake.plan[2]["date"] == _d(2) and fake.plan[5]["date"] == _d(3)
    assert fake.plan[3]["date"] == _d(0)                # la cena no se toca
    cli.post("/api/plan/2/posponer", json={"dias": -2})
    assert fake.plan[2]["date"] == _d(0) and fake.plan[5]["date"] == _d(1)


def test_notas(cli, fake):
    assert cli.get("/api/recetas/lentejas").json()["comentarios"] == []
    r = cli.post("/api/recetas/lentejas/nota", json={"texto": "  con menos sal "}).json()
    assert r == {"texto": "con menos sal", "fecha": "2026-10-01", "autor": "usuario"}
    assert fake.comentarios[0]["recipeId"] == "r1"
    cli.post("/api/recetas/lentejas/nota", json={"texto": "sin chorizo"})
    assert [n["texto"] for n in cli.get("/api/recetas/lentejas").json()["comentarios"]] == ["sin chorizo", "con menos sal"]
    assert cli.post("/api/recetas/lentejas/nota", json={"texto": ""}).status_code == 422


def test_valorar(cli, fake):
    assert cli.post("/api/recetas/lentejas/valoracion", json={"estrellas": 5}).status_code == 200
    assert fake.valorado == [("lentejas", 5)]


def test_compra_en_ha(cli, ha):
    assert cli.get("/api/compra").json()["items"] == [{"id": "i1", "texto": "Leche", "hecho": False}]
    cli.put("/api/compra/i1", json={"hecho": True})
    assert ha.items[0]["status"] == "completed"
    cli.post("/api/compra", json={"texto": "  Pan "})
    assert ha.items[1]["summary"] == "Pan"
    cli.delete("/api/compra/comprados")
    assert [i["summary"] for i in ha.items] == ["Pan"]


# --- Avisos ------------------------------------------------------------------------

class FakeNotify:
    """notify.* de HA: apunta lo que se manda."""

    def __init__(self):
        self.enviados: list[tuple[str, dict]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        dominio, servicio = req.url.path.split("/")[-2:]
        assert dominio == "notify"
        self.enviados.append((servicio, json.loads(req.content)))
        return httpx.Response(200, json=[])


def _ha_notify(f: FakeNotify) -> HA:
    return HA("http://ha", "t", "", transport=httpx.MockTransport(f))


def test_aviso_descongelar(fake, monkeypatch):
    from panel import avisos
    monkeypatch.setenv("HA_NOTIFY", "mobile_app_movil")
    monkeypatch.setenv("PANEL_URL", "https://comidas.example.com")
    m = Mealie("http://mealie", "t", transport=httpx.MockTransport(fake))
    n = FakeNotify()
    manana = HOY + dt.timedelta(days=1)
    with db.conexion() as c:
        assert avisos.avisa_descongelar(m, c, HOY, _ha_notify(n)) is False   # el pollo de mañana se cocina
        db.marca_del_congelador(c, _d(1), "dinner", "r2")
        db.mete_borrador(c, manana, "lunch", "r3", congelador=True)
        assert avisos.avisa_descongelar(m, c, HOY, _ha_notify(n)) is True
        db.marca_descongelado(c, manana, "dinner", True)
        db.marca_descongelado(c, manana, "lunch", True)
        assert avisos.avisa_descongelar(m, c, HOY, _ha_notify(n)) is False   # ya está en el frigo
    [(servicio, cuerpo)] = n.enviados
    assert servicio == "mobile_app_movil"
    assert cuerpo["title"] == "Pasa al frigo"
    assert cuerpo["message"] == "Tortilla (comida) y Pollo al curry (cena), para mañana."
    assert cuerpo["data"] == {"group": "comidas", "url": "https://comidas.example.com"}


def test_aviso_borrador(monkeypatch):
    from panel import avisos
    monkeypatch.setenv("HA_NOTIFY", "mobile_app_movil")
    n = FakeNotify()
    assert avisos.avisa_borrador(0, dt.date(2026, 10, 12), _ha_notify(n)) is False
    assert avisos.avisa_borrador(11, dt.date(2026, 10, 12), _ha_notify(n)) is True
    assert n.enviados[0][1]["message"] == "11 platos para la semana del 12 de octubre. Revísalo y apruébalo."


def test_aviso_sin_destino_o_con_ha_caido(monkeypatch):
    from panel import avisos
    monkeypatch.setenv("HA_NOTIFY", "")
    n = FakeNotify()
    assert avisos.envia("t", "m", _ha_notify(n)) is False and n.enviados == []
    monkeypatch.setenv("HA_NOTIFY", "mobile_app_movil")
    caido = HA("http://ha", "t", "", transport=httpx.MockTransport(lambda req: httpx.Response(500)))
    assert avisos.envia("t", "m", caido) is False
