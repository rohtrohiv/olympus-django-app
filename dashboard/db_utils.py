from contextlib import contextmanager
from django.db import connections, DEFAULT_DB_ALIAS


@contextmanager
def db_conn(alias=DEFAULT_DB_ALIAS):
    """Context manager that yields a DB cursor using Django's connection.

    Usage:
        with db_conn() as cursor:
            cursor.execute('SELECT ...')
            rows = cursor.fetchall()

    This uses Django's configured DATABASES settings and connection pooling.
    """
    conn = connections[alias]
    cursor = conn.cursor()
    try:
        yield cursor
    finally:
        try:
            cursor.close()
        except Exception:
            pass


def execute_query(sql, params=None, alias=DEFAULT_DB_ALIAS):
    """Execute a SQL query and return all rows.

    Example:
        rows = execute_query('SELECT 1')
    """
    with db_conn(alias) as cur:
        cur.execute(sql, params or [])
        return cur.fetchall()
