"""Home Assistant: la lista de la compra (entidad `todo`) y los avisos al móvil.

La lista es la que ya llega al móvil, así que el panel la usa directamente en vez
de la de Mealie: una sola lista, nada que sincronizar. Los avisos van por la app de
HA (`notify.mobile_app_…`), que ya está instalada y con permisos: no hace falta PWA
ni salida a internet desde el servidor.
"""

from __future__ import annotations

import httpx


class HA:
    def __init__(self, url: str, token: str, entidad: str, *, transport: httpx.BaseTransport | None = None):
        self.entidad = entidad
        self._c = httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
            transport=transport,
        )

    def __enter__(self) -> "HA":
        return self

    def __exit__(self, *exc) -> None:
        self._c.close()

    def _llama(self, dominio: str, servicio: str, datos: dict, *, respuesta: bool = False) -> dict:
        r = self._c.post(
            f"/api/services/{dominio}/{servicio}" + ("?return_response" if respuesta else ""),
            json=datos,
        )
        r.raise_for_status()
        return r.json()

    def _servicio(self, servicio: str, datos: dict, *, respuesta: bool = False) -> dict:
        return self._llama("todo", servicio, {"entity_id": self.entidad, **datos}, respuesta=respuesta)

    def notifica(self, destino: str, titulo: str, mensaje: str, url: str | None = None) -> None:
        """Notificación de la app de HA. `destino`: servicio notify sin el dominio
        (mobile_app_…). Con `url`, tocarla abre esa dirección. `group` las agrupa en iOS."""
        data: dict = {"group": "comidas"}
        if url:
            data["url"] = url
        self._llama("notify", destino, {"title": titulo, "message": mensaje, "data": data})

    def items(self) -> list[dict]:
        """[{uid, summary, status}] — status: needs_action | completed."""
        j = self._servicio("get_items", {"status": ["needs_action", "completed"]}, respuesta=True)
        return j["service_response"][self.entidad]["items"]

    def marca(self, uid: str, hecho: bool) -> None:
        self._servicio("update_item", {"item": uid, "status": "completed" if hecho else "needs_action"})

    def anade(self, texto: str) -> None:
        self._servicio("add_item", {"item": texto})

    def vacia_completados(self) -> None:
        """Borra de la lista todo lo ya comprado (HA lo guarda indefinidamente)."""
        self._servicio("remove_completed_items", {})
