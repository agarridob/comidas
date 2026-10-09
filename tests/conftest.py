import pytest


@pytest.fixture(autouse=True)
def db_temporal(tmp_path, monkeypatch):
    """Cada test con su propia SQLite vacía (nunca /var/lib/comidas)."""
    ruta = tmp_path / "comidas.db"
    monkeypatch.setenv("COMIDAS_DB", str(ruta))
    return ruta
