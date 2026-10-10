"""Cliente mínimo de la API de Mealie (v3.x). Mealie es la fuente de verdad: el
panel no guarda nada, solo lee y escribe aquí.

Endpoints validados contra el /openapi.json de Mealie 3.28.0 .
"""

from __future__ import annotations

import datetime as dt

import httpx

# Tipos de entrada del plan que el panel muestra como "comida" y "cena".
COMIDA, CENA = "lunch", "dinner"
TIPOS_PANEL = (COMIDA, CENA)


class Mealie:
    def __init__(self, url: str, token: str, *, transport: httpx.BaseTransport | None = None):
        self._c = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=10,
            transport=transport,
        )

    def __enter__(self) -> "Mealie":
        return self

    def __exit__(self, *exc) -> None:
        self._c.close()

    def _get(self, ruta: str, **params) -> dict:
        r = self._c.get(ruta, params=params)
        r.raise_for_status()
        return r.json()

    def _todas(self, ruta: str, **params) -> list[dict]:
        """GET paginado de Mealie: devuelve todos los `items` de todas las páginas."""
        out: list[dict] = []
        pagina = 1
        while True:
            j = self._get(ruta, page=pagina, perPage=100, **params)
            out.extend(j.get("items", []))
            if pagina >= (j.get("total_pages") or 1):
                return out
            pagina += 1

    # --- Plan --------------------------------------------------------------------

    def plan(self, desde: dt.date, hasta: dt.date) -> list[dict]:
        """Entradas del plan entre dos fechas (ambas incluidas)."""
        return self._todas(
            "/api/households/mealplans",
            start_date=desde.isoformat(),
            end_date=hasta.isoformat(),
            orderBy="date",
            orderDirection="asc",
        )

    def _actualiza(self, entrada: dict, **cambios) -> dict:
        """El PUT de Mealie reemplaza la entrada entera: se reenvían todos los campos
        de UpdatePlanEntry tal cual, con `cambios` aplicados."""
        cuerpo = {
            k: entrada.get(k)
            for k in ("id", "groupId", "userId", "date", "entryType", "title", "text", "recipeId")
        }
        cuerpo.update(cambios)
        r = self._c.put(f"/api/households/mealplans/{entrada['id']}", json=cuerpo)
        r.raise_for_status()
        return r.json()

    def mueve_entrada(self, entrada: dict, dias: int) -> dict:
        """Desplaza una entrada del plan `dias` días (negativo = adelantar)."""
        nueva = dt.date.fromisoformat(entrada["date"]) + dt.timedelta(days=dias)
        return self._actualiza(entrada, date=nueva.isoformat())

    def crea_entrada(self, fecha: dt.date, tipo: str, receta_id: str, texto: str = "") -> dict:
        r = self._c.post(
            "/api/households/mealplans",
            json={"date": fecha.isoformat(), "entryType": tipo, "recipeId": receta_id, "text": texto},
        )
        r.raise_for_status()
        return r.json()

    def borra_entrada(self, id_: int) -> None:
        r = self._c.delete(f"/api/households/mealplans/{id_}")
        r.raise_for_status()

    def entrada(self, id_: int) -> dict:
        return self._get(f"/api/households/mealplans/{id_}")

    # --- Recetas -----------------------------------------------------------------

    def receta(self, slug: str) -> dict:
        """Receta completa (con recipeIngredient)."""
        return self._get(f"/api/recipes/{slug}")

    def recetas(self) -> list[dict]:
        """Todas las recetas (RecipeSummary: id, slug, name, tags, rating…)."""
        return self._todas("/api/recipes")

    # --- Valoraciones ------------------------------------------------------------

    def yo(self) -> dict:
        return self._get("/api/users/self")

    def mis_valoraciones(self) -> dict[str, float]:
        """recipe_id -> estrellas del usuario del token."""
        j = self._get("/api/users/self/ratings")
        return {r["recipeId"]: r["rating"] for r in j.get("ratings", []) if r.get("rating")}

    # --- Comentarios (las notas de "cómo salió") ---------------------------------

    def comentarios(self) -> list[dict]:
        """Todos los comentarios de recetas, del más nuevo al más viejo."""
        return self._todas("/api/comments", orderBy="createdAt", orderDirection="desc")

    def comentarios_receta(self, slug: str) -> list[dict]:
        return self._get(f"/api/recipes/{slug}/comments")

    def comenta(self, receta_id: str, texto: str) -> dict:
        r = self._c.post("/api/comments", json={"recipeId": receta_id, "text": texto})
        r.raise_for_status()
        return r.json()

    def valora(self, user_id: str, slug: str, estrellas: float) -> None:
        r = self._c.post(f"/api/users/{user_id}/ratings/{slug}", json={"rating": estrellas})
        r.raise_for_status()


# --- Lógica pura (testeable sin red) ---------------------------------------------

def tiene_etiqueta(entrada: dict, etiqueta: str) -> bool:
    receta = entrada.get("recipe") or {}
    etiqueta = etiqueta.lower()
    return any(
        etiqueta in ((t.get("slug") or "").lower(), (t.get("name") or "").lower())
        for t in receta.get("tags") or []
    )


def a_posponer(plan: list[dict], entrada: dict) -> list[dict]:
    """Entradas que se desplazan al posponer `entrada`: ella y todas las posteriores
    del mismo tipo (comida o cena). Como en Supper-Board, "hoy no cocinamos" corre
    toda la secuencia un día, en vez de pisar la entrada de mañana."""
    fecha = entrada["date"]
    return [
        e for e in plan
        if e["entryType"] == entrada["entryType"] and e["date"] >= fecha
    ]
