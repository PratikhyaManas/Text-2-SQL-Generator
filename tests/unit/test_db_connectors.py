from src.db.connectors import resolve_database_target


def test_resolve_database_target_for_sqlite_path():
    resolved = resolve_database_target("data/sample.db")

    assert resolved.backend == "sqlite"
    assert resolved.target == "data/sample.db"


def test_resolve_database_target_for_postgresql_url():
    resolved = resolve_database_target("postgresql://user:pass@localhost:5432/appdb")

    assert resolved.backend == "postgresql"


def test_resolve_database_target_for_mysql_url():
    resolved = resolve_database_target("mysql://user:pass@localhost:3306/appdb")

    assert resolved.backend == "mysql"
