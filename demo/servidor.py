"""Panel con datos de ejemplo, sin Mealie ni Home Assistant (para las capturas del README).

    .venv/bin/python -m demo.servidor          # http://127.0.0.1:8765

Mealie y HA se simulan en memoria con recetas inventadas; el plan se coloca alrededor
de hoy y la SQLite es temporal, así que cada arranque empieza igual.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import tempfile

import httpx

os.environ["COMIDAS_DB"] = os.path.join(tempfile.mkdtemp(prefix="comidas-demo-"), "comidas.db")
os.environ.setdefault("MEALIE_PUBLIC_URL", "https://mealie.example.com")

from panel import app as app_mod  # noqa: E402
from panel import db, generar  # noqa: E402
from panel.ha import HA  # noqa: E402
from panel.mealie import Mealie  # noqa: E402

HOY = dt.date.today()
LUNES = HOY - dt.timedelta(days=HOY.weekday())

# nombre, categorías, etiquetas, minutos, raciones, ingredientes
RECETAS = [
    ("Lentejas estofadas con verduras", "comida", "legumbres congelable", 50, 2,
     ["150 g de lentejas", "1 zanahoria", "1/2 cebolla", "1 pimiento verde", "1 diente de ajo", "1 hoja de laurel", "Aceite de oliva", "Sal"]),
    ("Garbanzos con espinacas", "comida", "legumbres rapida", 20, 1,
     ["1 bote de garbanzos cocidos", "100 g de espinacas", "1 diente de ajo", "1 cucharadita de pimentón", "Aceite de oliva"]),
    ("Merluza al horno con patata", "comida", "pescado horno", 35, 1,
     ["200 g de merluza", "1 patata", "1/2 cebolla", "Aceite de oliva", "Sal"]),
    ("Salmón a la plancha con ensalada", "cena", "pescado rapida", 15, 1,
     ["1 lomo de salmón", "1 bolsa de canónigos", "1 tomate", "Aceite de oliva"]),
    ("Pollo al curry con arroz", "comida", "pollo arroz congelable", 40, 2,
     ["2 pechugas de pollo", "150 g de arroz basmati", "200 ml de leche de coco", "1 cebolla", "Curry"]),
    ("Tortilla de patata", "cena", "huevo", 30, 1,
     ["3 huevos", "2 patatas", "1/2 cebolla", "Aceite de oliva", "Sal"]),
    ("Fajitas de pollo con guacamole", "cena", "pollo rapida", 20, 1,
     ["2 tortillas integrales", "150 g de pollo", "1/2 pimiento rojo", "1/2 aguacate", "Queso rallado (opcional)"]),
    ("Espaguetis con tomate y atún", "comida", "pasta rapida", 15, 1,
     ["100 g de espaguetis", "1 lata de atún", "200 g de tomate triturado", "Orégano"]),
    ("Crema de calabaza", "cena", "crema congelable", 30, 2,
     ["500 g de calabaza", "1 patata", "1 cebolla", "Caldo de verduras"]),
    ("Revuelto de setas y gambas", "cena", "huevo marisco rapida", 15, 1,
     ["2 huevos", "100 g de setas", "80 g de gambas peladas", "1 diente de ajo"]),
    ("Pechuga de pavo con verduras", "cena", "carne rapida", 20, 1,
     ["1 filete de pechuga de pavo", "1/2 calabacín", "1/2 pimiento rojo", "Aceite de oliva"]),
    ("Ensalada de quinoa", "comida cena", "ensalada rapida", 15, 1,
     ["60 g de quinoa", "1 tomate", "1/2 pepino", "50 g de queso feta"]),
    ("Arroz con verduras", "comida", "arroz", 35, 1,
     ["80 g de arroz", "1/2 pimiento rojo", "50 g de guisantes", "1 zanahoria"]),
    ("Alubias blancas con chorizo", "comida", "legumbres congelable", 60, 4,
     ["1 bote de alubias blancas", "1 chorizo", "1 cebolla", "1 cucharadita de pimentón"]),
    ("Bacalao con tomate", "comida", "pescado", 30, 1,
     ["200 g de bacalao desalado", "300 g de tomate triturado", "1/2 cebolla"]),
    ("Alitas de pollo en la airfryer", "cena", "pollo airfryer", 25, 1,
     ["400 g de alitas de pollo", "Pimentón", "Ajo en polvo"]),
    ("Macarrones gratinados", "comida", "pasta horno", 30, 1,
     ["100 g de macarrones", "200 g de tomate triturado", "50 g de queso rallado"]),
    ("Hamburguesa de ternera con ensalada", "cena", "carne", 20, 1,
     ["1 hamburguesa de ternera", "1 tomate", "Lechuga"]),
]
PASOS = ["Prepara y corta todos los ingredientes.", "Cocina a fuego medio hasta que esté hecho.",
         "Ajusta de sal y sirve."]


def _slug(nombre: str) -> str:
    return nombre.lower().replace(" ", "-").translate(str.maketrans("áéíóúñ", "aeioun"))


class MealieDemo:
    def __init__(self):
        self.recetas = {}
        for i, (nombre, cats, tags, mins, raciones, ings) in enumerate(RECETAS):
            r = {"id": f"r{i}", "slug": _slug(nombre), "name": nombre, "image": None,
                 "totalTime": f"{mins} minutes", "recipeServings": raciones,
                 "recipeCategory": [{"name": c, "slug": c} for c in cats.split()],
                 "tags": [{"name": t, "slug": t} for t in tags.split()]}
            self.recetas[r["slug"]] = (r, ings)
        self.ratings = {"r0": 5.0, "r2": 4.0, "r5": 5.0, "r6": 4.0, "r8": 3.0, "r14": 4.0}
        self.comentarios = [{"id": "c0", "recipeId": "r0", "text": "Mejor con un poco de comino",
                             "createdAt": f"{HOY - dt.timedelta(days=9)}T21:00:00", "user": {"username": "demo"}}]
        self.plan: dict[int, dict] = {}
        r = lambda slug: self.recetas[slug][0]  # noqa: E731
        # Plan aprobado alrededor de hoy (días respecto a hoy): lo pasado queda atenuado,
        # "Puntúa" pide valorar lo de ayer y mañana se cena el curry del congelador.
        plan = {
            -4: ("lentejas-estofadas-con-verduras", "revuelto-de-setas-y-gambas"),
            -3: ("merluza-al-horno-con-patata", "fajitas-de-pollo-con-guacamole"),
            -2: ("pollo-al-curry-con-arroz", "salmon-a-la-plancha-con-ensalada"),
            -1: ("garbanzos-con-espinacas", "hamburguesa-de-ternera-con-ensalada"),
            0: ("espaguetis-con-tomate-y-atun", "tortilla-de-patata"),
            1: ("bacalao-con-tomate", "pollo-al-curry-con-arroz"),
            2: ("lentejas-estofadas-con-verduras", "pechuga-de-pavo-con-verduras"),
            3: ("arroz-con-verduras", "alitas-de-pollo-en-la-airfryer"),
        }
        self.del_congelador = []
        for d, (comida, cena) in plan.items():
            fecha = HOY + dt.timedelta(days=d)
            if fecha < LUNES or fecha > LUNES + dt.timedelta(days=6):
                continue
            for tipo, slug in (("lunch", comida), ("dinner", cena)):
                if slug:
                    self._crea(fecha, tipo, r(slug)["id"])
            if d == 1:
                self.del_congelador.append((str(fecha), "dinner", r(cena)["id"]))

    def _crea(self, fecha: dt.date | str, tipo: str, receta_id: str, texto: str = "") -> dict:
        id_ = len(self.plan) + 1
        receta = next(r for r, _ in self.recetas.values() if r["id"] == receta_id)
        self.plan[id_] = {"id": id_, "date": str(fecha), "entryType": tipo, "title": "", "text": texto,
                          "recipeId": receta_id, "recipe": receta, "groupId": "g", "userId": "u", "householdId": "h"}
        return self.plan[id_]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        p, m = req.url.path, req.method
        if p == "/api/households/mealplans":
            if m == "POST":
                c = json.loads(req.content)
                return httpx.Response(201, json=self._crea(c["date"], c["entryType"], c["recipeId"], c.get("text", "")))
            a, b = req.url.params["start_date"], req.url.params["end_date"]
            es = sorted((e for e in self.plan.values() if a <= e["date"] <= b), key=lambda e: e["date"])
            return httpx.Response(200, json={"items": es, "total_pages": 1})
        if p.startswith("/api/households/mealplans/"):
            id_ = int(p.rsplit("/", 1)[1])
            if m == "PUT":
                self.plan[id_]["date"] = json.loads(req.content)["date"]
            return httpx.Response(200, json=self.plan[id_])
        if p == "/api/users/self":
            return httpx.Response(200, json={"id": "u"})
        if p == "/api/users/self/ratings":
            return httpx.Response(200, json={"ratings": [{"recipeId": k, "rating": v} for k, v in self.ratings.items()]})
        if p.startswith("/api/users/u/ratings/"):
            rid = self.recetas[p.rsplit("/", 1)[1]][0]["id"]
            self.ratings[rid] = json.loads(req.content)["rating"]
            return httpx.Response(200, json={})
        if p == "/api/comments":
            if m == "POST":
                c = json.loads(req.content)
                nuevo = {"id": f"c{len(self.comentarios)}", "recipeId": c["recipeId"], "text": c["text"],
                         "createdAt": f"{HOY}T20:00:00", "user": {"username": "demo"}}
                self.comentarios.append(nuevo)
                return httpx.Response(201, json=nuevo)
            return httpx.Response(200, json={"items": self.comentarios, "total_pages": 1})
        if p == "/api/recipes":
            return httpx.Response(200, json={"items": [r for r, _ in self.recetas.values()], "total_pages": 1})
        if p.startswith("/api/recipes/"):
            partes = p.split("/")
            r, ings = self.recetas[partes[3]]
            if p.endswith("/comments"):
                return httpx.Response(200, json=[c for c in self.comentarios if c["recipeId"] == r["id"]])
            return httpx.Response(200, json={
                **r, "recipeIngredient": [{"note": i} for i in ings], "prepTime": "10 minutes", "notes": [],
                "recipeInstructions": [{"title": "", "text": t} for t in PASOS]})
        return httpx.Response(404, json={"path": p})


class HADemo:
    ENTIDAD = "todo.lista_de_la_compra"

    def __init__(self):
        self.items = [{"uid": f"i{n}", "summary": s, "status": st} for n, (s, st) in enumerate([
            ("Leche", "needs_action"), ("Pan integral", "needs_action"), ("Plátanos", "needs_action"),
            ("Yogures naturales", "completed"), ("Café", "completed")])]

    def __call__(self, req: httpx.Request) -> httpx.Response:
        datos = json.loads(req.content)
        servicio = req.url.path.rsplit("/", 1)[1]
        if servicio == "get_items":
            return httpx.Response(200, json={"service_response": {self.ENTIDAD: {"items": self.items}}})
        if servicio == "update_item":
            next(i for i in self.items if i["uid"] == datos["item"])["status"] = datos["status"]
        elif servicio == "add_item":
            self.items.append({"uid": f"i{len(self.items)}", "summary": datos["item"], "status": "needs_action"})
        elif servicio == "remove_completed_items":
            self.items = [i for i in self.items if i["status"] != "completed"]
        return httpx.Response(200, json=[])


def prepara() -> None:
    mealie, ha = MealieDemo(), HADemo()
    app_mod._cliente = lambda: Mealie("http://mealie", "t", transport=httpx.MockTransport(mealie))
    app_mod._ha = lambda: HA("http://ha", "t", HADemo.ENTIDAD, transport=httpx.MockTransport(ha))
    with db.conexion() as c, app_mod._cliente() as m:
        db.suma_congelador(c, "r13", 3)   # alubias de una semana anterior
        for fecha, tipo, receta_id in mealie.del_congelador:
            db.marca_del_congelador(c, fecha, tipo, receta_id)
        generar.sincroniza(m, c, HOY)
        generar.genera(m, c, LUNES + dt.timedelta(days=7), HOY, random.Random(7))


if __name__ == "__main__":
    import uvicorn

    prepara()
    uvicorn.run(app_mod.app, host="127.0.0.1", port=int(os.environ.get("PORT", "8765")))
