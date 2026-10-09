"""Parser de ingredientes en castellano y agregado entre recetas."""

from __future__ import annotations

from fractions import Fraction

import pytest

from panel import ingredientes as I


@pytest.mark.parametrize("texto, nombre, cantidad, unidad", [
    ("500 g de champiñones", "champiñones", 500, "g"),
    ("2 dientes de ajo", "ajo", 2, "diente"),
    ("1/2 cebolla (opcional)", "cebolla", Fraction(1, 2), None),
    ("Un chorrito de aceite de oliva", "aceite de oliva", 1, "chorrito"),
    ("1 bote grande de espárragos blancos", "espárragos blancos", 1, "bote"),
    ("2-3 tomates maduros", "tomates maduros", None, None),    # los rangos no se suman
    ("Perejil", "Perejil", None, None),
    ("2 pimientos rojos asados (de bote o asados en casa)", "pimientos rojos asados", 2, None),
    ("200 ml de leche evaporada, fría", "leche evaporada", 200, "ml"),
    ("1,5 kg de patatas", "patatas", Fraction(3, 2), "kg"),
])
def test_parsea(texto, nombre, cantidad, unidad):
    linea = I.parsea({"note": texto})
    assert (linea.nombre, linea.cantidad, linea.unidad) == (nombre, cantidad, unidad)


def test_parsea_usa_lo_ya_parseado_en_mealie():
    linea = I.parsea({"quantity": 2, "unit": {"name": "dientes"}, "food": {"name": "ajo"}, "display": "2 dientes ajo"})
    assert (linea.nombre, linea.cantidad, linea.unidad) == ("ajo", 2, "diente")


def test_opcional_y_alternativas():
    assert I.parsea({"note": "1/2 cebolla (opcional)"}).opcional
    ls = I.parsea_todas({"note": "Opción salteado: 1 calabacín, 1 pimiento rojo y 200 g de judías verdes"})
    assert [(l.nombre, l.cantidad, l.unidad) for l in ls] == [
        ("calabacín", 1, None), ("pimiento rojo", 1, None), ("judías verdes", 200, "g")]
    assert all(l.opcional for l in ls)
    ls = I.parsea_todas({"note": "Proteína a elegir: pechuga de pollo, o filete de ternera"})
    assert [(l.nombre, l.unidad) for l in ls] == [("pechuga de pollo", None), ("ternera", "filete")]


@pytest.mark.parametrize("a, b", [("tomates", "tomate"), ("champiñones", "champiñón"), ("limones", "Limón"),
                                  ("pimientos verdes", "pimiento verde")])
def test_clave_junta_plurales_y_acentos(a, b):
    assert I.clave(a) == I.clave(b)


def test_basicos():
    assert I.es_basico("Aceite de oliva virgen extra") and I.es_basico("Sal") and I.es_basico("pimienta negra")
    assert not I.es_basico("salmón") and not I.es_basico("Salchichas")


def _receta(nombre, *lineas):
    return {"name": nombre, "recipeIngredient": [{"note": l, "display": l} for l in lineas]}


def test_agrega_suma_por_unidad_y_marca_basicos():
    items = {i.clave: i for i in I.agrega([
        _receta("Ensalada", "2 latas de atún", "2 tomates", "Sal", "1/2 cebolla (opcional)"),
        _receta("Pasta", "1 lata de atún", "1 tomate", "100 g de tomate seco", "Sal"),
        _receta("Pisto", "1 cebolla", "Para la salsa:"),
    ])}
    assert items["atun"].texto() == "Atún (3 latas)" and items["atun"].recetas == ["Ensalada", "Pasta"]
    assert items["tomate"].texto() == "Tomates (3)"
    assert items["tomate seco"].texto() == "Tomate seco (100 g)"
    assert items["sal"].basico and items["sal"].texto() == "Sal"
    assert not items["cebolla"].opcional            # en el pisto no es opcional
    assert items["cebolla"].texto() == "Cebolla (1,5)"
    assert list(items)[-1] == "sal"                  # los básicos al final
    assert not any("salsa" in k for k in items)      # "Para la salsa:" es una cabecera
