"""Configuración del plan semanal: qué huecos se planifican y reglas de variedad."""

from __future__ import annotations

from .mealie import CENA, COMIDA

# Huecos por día de la semana (0 = lunes). Sábado libre; viernes solo comida.
HUECOS: dict[int, tuple[str, ...]] = {
    0: (COMIDA, CENA),
    1: (COMIDA, CENA),
    2: (COMIDA, CENA),
    3: (COMIDA, CENA),
    4: (COMIDA,),
    6: (COMIDA, CENA),
}

# Categoría de Mealie que debe tener una receta para cada hueco (las que tienen
# las dos valen para ambos; sin ninguna de las dos, también).
CATEGORIA = {COMIDA: "comida", CENA: "cena"}

# Reglas de variedad por semana, por slug de etiqueta de Mealie.
MINIMOS = {"pescado": 2, "legumbres": 2}
MAXIMOS = {
    # Proteínas: como mucho 3 de cada una, salvo el pollo, sin límite.
    "carne": 3, "pescado": 3, "marisco": 3, "huevo": 3, "legumbres": 3,
    "pasta": 2, "arroz": 2,
}

# Peso de cada receta en el sorteo = estrellas². Sin valorar (o 0) cuenta como 3★:
# una de 5★ sale ~2,8 veces más que una sin valorar; una de 1★, 9 veces menos.
ESTRELLAS_SIN_VALORAR = 3.0

# Congelador: las recetas con esta etiqueta se cocinan para sus raciones de Mealie
# (p. ej. 2 o 4); al pasar el día, (raciones - 1) entran al inventario del congelador
# y el borrador de la semana siguiente las coloca antes de sortear el resto. La
# víspera de cada una, aviso de descongelar.
ETIQUETA_CONGELABLE = "congelable"

# No repetir recetas planificadas en los últimos N días.
DIAS_SIN_REPETIR = 14

# --- Compra ----------------------------------------------------------------------------
# Básicos de despensa: al pasar los ingredientes del plan a la compra salen
# desmarcados (se suelen tener). Se compara sin acentos ni plurales y por prefijo:
# "aceite" cubre "aceite de oliva virgen extra".
BASICOS = (
    "sal", "pimienta", "aceite", "vinagre", "agua", "azúcar", "harina",
    "orégano", "comino", "pimentón", "laurel", "tomillo", "romero", "curry", "cúrcuma",
    "canela", "nuez moscada", "ajo en polvo", "cebolla en polvo", "especias", "caldo",
)
# Días de plan aprobado (desde hoy) cuyos ingredientes se proponen para la compra.
DIAS_COMPRA = 7

# --- Avisos ------------------------------------------------------------------------------
# Servicio notify de HA al que van los avisos (la app de HA del móvil, p. ej.
# "mobile_app_mi_movil"). Se pone con HA_NOTIFY en el .env; vacío, no se avisa.
AVISOS_NOTIFY = ""
# Lo que abre tocar el aviso (PANEL_URL en el .env); vacío, el aviso no abre nada.
PANEL_URL = ""
