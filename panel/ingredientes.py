"""Ingredientes del plan → lista de la compra.

En Mealie casi todos los ingredientes son texto libre ("500 g de champiñones",
"2 dientes de ajo", "Perejil"): el parser de Mealie está pensado para inglés. Aquí
hay un parser mínimo para castellano que separa cantidad, unidad y alimento lo
justo para juntar lo repetido entre recetas ("Ajo (3 dientes)").
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from fractions import Fraction

from . import config

# Unidades reconocidas: forma escrita → forma canónica (singular, plural).
_UNIDADES = {
    "g": ("g", "g"), "gr": ("g", "g"), "grs": ("g", "g"), "gramo": ("g", "g"), "gramos": ("g", "g"),
    "kg": ("kg", "kg"), "kilo": ("kg", "kg"), "kilos": ("kg", "kg"),
    "ml": ("ml", "ml"), "cl": ("cl", "cl"), "l": ("l", "l"), "litro": ("l", "l"), "litros": ("l", "l"),
    "cucharada": ("cucharada", "cucharadas"), "cucharadas": ("cucharada", "cucharadas"), "cda": ("cucharada", "cucharadas"),
    "cdas": ("cucharada", "cucharadas"),
    "cucharadita": ("cucharadita", "cucharaditas"), "cucharaditas": ("cucharadita", "cucharaditas"),
    "cdta": ("cucharadita", "cucharaditas"), "cdtas": ("cucharadita", "cucharaditas"),
    "taza": ("taza", "tazas"), "tazas": ("taza", "tazas"), "vaso": ("vaso", "vasos"), "vasos": ("vaso", "vasos"),
    "lata": ("lata", "latas"), "latas": ("lata", "latas"), "bote": ("bote", "botes"), "botes": ("bote", "botes"),
    "diente": ("diente", "dientes"), "dientes": ("diente", "dientes"),
    "hoja": ("hoja", "hojas"), "hojas": ("hoja", "hojas"), "rama": ("rama", "ramas"), "ramas": ("rama", "ramas"),
    "ramita": ("ramita", "ramitas"), "ramitas": ("ramita", "ramitas"),
    "puñado": ("puñado", "puñados"), "puñados": ("puñado", "puñados"), "pizca": ("pizca", "pizcas"),
    "filete": ("filete", "filetes"), "filetes": ("filete", "filetes"),
    "loncha": ("loncha", "lonchas"), "lonchas": ("loncha", "lonchas"),
    "rodaja": ("rodaja", "rodajas"), "rodajas": ("rodaja", "rodajas"),
    "sobre": ("sobre", "sobres"), "sobres": ("sobre", "sobres"),
    "paquete": ("paquete", "paquetes"), "paquetes": ("paquete", "paquetes"),
    "trozo": ("trozo", "trozos"), "trozos": ("trozo", "trozos"),
    "bolsa": ("bolsa", "bolsas"), "bolsas": ("bolsa", "bolsas"),
    "chorrito": ("chorrito", "chorritos"), "chorro": ("chorro", "chorros"),
    "manojo": ("manojo", "manojos"), "manojos": ("manojo", "manojos"),
    "tarrina": ("tarrina", "tarrinas"), "tarrinas": ("tarrina", "tarrinas"),
    "tarro": ("tarro", "tarros"), "tarros": ("tarro", "tarros"),
    "cabeza": ("cabeza", "cabezas"), "cabezas": ("cabeza", "cabezas"),
    "bandeja": ("bandeja", "bandejas"), "bandejas": ("bandeja", "bandejas"),
}
_PALABRAS_NUM = {"un": 1, "una": 1, "uno": 1, "medio": Fraction(1, 2), "media": Fraction(1, 2),
                 "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6}
_FRACCIONES = {"½": Fraction(1, 2), "¼": Fraction(1, 4), "¾": Fraction(3, 4), "⅓": Fraction(1, 3)}

_NUM = r"(?:\d+(?:[.,]\d+)?(?:\s*/\s*\d+)?|[½¼¾⅓])"
_RE = re.compile(
    rf"^\s*(?P<cant>{_NUM}(?:\s*(?:-|a)\s*{_NUM})?|(?:{'|'.join(_PALABRAS_NUM)})\b)?\s*"
    rf"(?P<unidad>(?:{'|'.join(sorted(map(re.escape, _UNIDADES), key=len, reverse=True))})\b\.?)?\s*"
    r"(?:(?:grandes?|pequeñ[oa]s?|median[oa]s?)\s+)?(?:de\s+)?(?P<nombre>.*)$",
    re.IGNORECASE,
)


def _sin_acentos(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _singular(palabra: str) -> str:
    # -es solo tras las consonantes que lo piden (champiñones, limones, nueces…);
    # si no, basta quitar la -s (tomates → tomate, verdes → verde).
    if len(palabra) > 4 and palabra.endswith("es") and palabra[-3] in "nrlzjsy":
        return palabra[:-2]
    if len(palabra) > 3 and palabra.endswith("s"):
        return palabra[:-1]
    return palabra


def clave(nombre: str) -> str:
    """Forma normalizada para juntar ingredientes: minúsculas, sin acentos, singular."""
    s = _sin_acentos(nombre.lower())
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-z0-9ñ ]+", " ", s)
    return " ".join(_singular(p) for p in s.split())


def _num(texto: str) -> Fraction | None:
    t = texto.strip().lower()
    if t in _PALABRAS_NUM:
        return Fraction(_PALABRAS_NUM[t])
    if t in _FRACCIONES:
        return _FRACCIONES[t]
    t = t.replace(",", ".").replace(" ", "")
    try:
        return Fraction(t) if "/" in t else Fraction(t).limit_denominator(100)
    except (ValueError, ZeroDivisionError):
        return None


@dataclass
class Linea:
    nombre: str                     # "champiñones"
    cantidad: Fraction | None       # 500 (None: sin cantidad o rango)
    unidad: str | None              # "g" (canónica, singular) o None = piezas
    opcional: bool = False
    texto: str = ""                 # línea original


# "Opción salteado: 1 calabacín, 1 pimiento…", "Opcional: …", "Proteína a elegir: …":
# alternativas o extras. Se separan en ingredientes sueltos, todos opcionales.
_ALTERNATIVA = re.compile(r"^\s*(opci[oó]n[^:]*|opcional|[^:]*\ba elegir)\s*:\s*(?P<resto>.+)$", re.IGNORECASE)


def parsea_todas(ing: dict) -> list[Linea]:
    """Un ingrediente de Mealie a una o varias Lineas (las alternativas se separan)."""
    texto = (ing.get("display") or ing.get("note") or "").strip()
    m = None if ing.get("food") else _ALTERNATIVA.match(texto)
    if not m:
        linea = parsea(ing)
        return [linea] if linea else []
    out = []
    for parte in re.split(r",\s*|\s+y\s+", m.group("resto")):
        linea = parsea({"note": re.sub(r"^[ou]\s+", "", parte.strip(), flags=re.IGNORECASE)})
        if linea:
            linea.opcional = True
            out.append(linea)
    return out


def parsea(ing: dict) -> Linea | None:
    """Un ingrediente de Mealie (recipeIngredient) a Linea. None si no hay nada útil."""
    texto = (ing.get("display") or ing.get("note") or "").strip()
    if ing.get("food"):   # parseado en Mealie: se usa tal cual
        cant = Fraction(ing["quantity"]).limit_denominator(100) if ing.get("quantity") else None
        u = (ing.get("unit") or {}).get("name")
        unidad = _UNIDADES.get(u.lower(), (u, u))[0] if u else None
        return Linea(ing["food"]["name"], cant, unidad, "opcional" in texto.lower(), texto)
    if not texto or texto.endswith(":"):   # vacío o cabecera de sección ("Para la salsa:")
        return None
    opcional = bool(re.search(r"\bopcional\b", texto, re.IGNORECASE))
    m = _RE.match(texto)
    cant_txt, unidad_txt, nombre = m.group("cant"), m.group("unidad"), m.group("nombre")
    # El nombre termina antes del primer paréntesis o coma ("tomates (maduros), en dados").
    nombre = re.split(r"[(,;]| para | al gusto", nombre, maxsplit=1)[0].strip(" .-")
    if not nombre:
        return None
    cantidad = None
    if cant_txt and not re.search(r"-|\ba\b", cant_txt):   # los rangos ("2-3") no se suman
        cantidad = _num(cant_txt)
    unidad = _UNIDADES[unidad_txt.lower().rstrip(".")][0] if unidad_txt else None
    return Linea(nombre, cantidad, unidad, opcional, texto)


def es_basico(nombre: str) -> bool:
    k = clave(nombre)
    return any(k == b or k.startswith(b + " ") for b in map(clave, config.BASICOS))


def _fmt(cant: Fraction) -> str:
    if cant.denominator == 1:
        return str(cant.numerator)
    if cant == Fraction(1, 2):
        return "½"
    return f"{float(cant):.2f}".rstrip("0").rstrip(".").replace(".", ",")


@dataclass
class Item:
    clave: str
    nombre: str
    cantidades: dict[str | None, Fraction] = field(default_factory=dict)
    sin_cantidad: bool = False
    recetas: list[str] = field(default_factory=list)
    opcional: bool = True           # solo si es opcional en todas las recetas
    basico: bool = False

    def cantidad(self) -> str:
        partes = []
        for unidad, cant in self.cantidades.items():
            u = "" if unidad is None else " " + (_UNIDADES.get(unidad, (unidad, unidad))[1 if cant > 1 else 0])
            partes.append(f"{_fmt(cant)}{u}")
        return " + ".join(partes)

    def texto(self) -> str:
        """Lo que se añade a la lista de HA: "Champiñones (500 g)"."""
        nombre = self.nombre[:1].upper() + self.nombre[1:]
        c = self.cantidad()
        return f"{nombre} ({c})" if c else nombre


def agrega(recetas: list[dict]) -> list[Item]:
    """Junta los ingredientes de varias recetas completas de Mealie."""
    items: dict[str, Item] = {}
    for r in recetas:
        for ing in r.get("recipeIngredient") or []:
            if ing.get("title") and not (ing.get("note") or ing.get("food")):
                continue   # cabecera de sección ("Para la salsa")
            for linea in parsea_todas(ing):
                k = clave(linea.nombre)
                if not k:
                    continue
                it = items.setdefault(k, Item(k, linea.nombre, basico=es_basico(linea.nombre)))
                if linea.cantidad is not None:
                    it.cantidades[linea.unidad] = it.cantidades.get(linea.unidad, Fraction(0)) + linea.cantidad
                else:
                    it.sin_cantidad = True
                if r["name"] not in it.recetas:
                    it.recetas.append(r["name"])
                it.opcional = it.opcional and linea.opcional
    return sorted(items.values(), key=lambda i: (i.basico, i.clave))
