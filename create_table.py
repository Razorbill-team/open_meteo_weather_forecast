"""Create the empty weather forecast table WEATHER_FORECAST_TABLE (create_forecast_table.sql) in WEATHER_DB_URL, in one
transaction. A table that already exists is left as it is (nothing is dropped or replaced) and reported. Checks the
table's columns against open_meteo.COLUMNS (+ inserted_at), so the SQL and the code cannot drift apart.

    python create_table.py
"""

from __future__ import annotations

import sqlalchemy as sa

import open_meteo as om


def check_columns(c: sa.Connection) -> int:
    """Stop if the table's columns differ from open_meteo.COLUMNS (+ inserted_at); returns the number of columns."""
    schema, name = om.table()
    cols = c.execute(sa.text("select column_name from information_schema.columns where table_schema = :s and "
                             "table_name = :n order by ordinal_position"), {"s": schema, "n": name}).scalars().all()
    if sorted(cols) != sorted([*om.COLUMNS, "inserted_at"]):
        raise SystemExit(f"columns of {schema}.{name} differ from open_meteo.COLUMNS: {cols}")
    return len(cols)


def main() -> None:
    """Create the forecast table unless it exists."""
    eng = om.engine()
    schema, name = om.table()
    with eng.connect() as c:
        if c.execute(sa.text("select to_regnamespace(:s) is null"), {"s": f'"{schema}"'}).scalar():
            raise SystemExit(f"schema {schema} does not exist; create it first (or use an existing one)")
        if c.execute(sa.text("select to_regclass(:t) is not null"), {"t": om.quoted()}).scalar():
            check_columns(c)
            print(f"{schema}.{name} already exists (columns OK); left as it is")
            return
    ddl = (om.HERE / "create_forecast_table.sql").read_text(encoding="utf-8")
    with eng.begin() as c:
        c.execute(sa.text(ddl.format(schema=schema, table=om.quoted(), name=name)))
        n = check_columns(c)                                 # a SystemExit here rolls the creation back
    print(f"created {schema}.{name} ({n} columns, empty)")


if __name__ == "__main__":
    main()
