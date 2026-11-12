"""Database adapter supporting PostgreSQL and SQLite."""
import os
import sqlite3
import threading
from contextlib import closing

try:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

from db_config import get_database_config


def get_connection(store_name=None, sqlite_path=None):
    """Get appropriate database connection based on configuration.
    
    Returns either a SQLite connection or a PostgreSQL connection wrapper.
    Both support the same interface: execute(), fetchone(), fetchall(), close()
    """
    config = get_database_config()
    
    if config['type'] == 'sqlite':
        # SQLite connection
        if not sqlite_path:
            raise ValueError("sqlite_path required when DATABASE_URL not set")
        os.makedirs(os.path.dirname(sqlite_path) or ".", exist_ok=True)
        db = sqlite3.connect(sqlite_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        if store_name == 'wallet':
            db.execute("PRAGMA secure_delete=ON")
        return db
    else:
        # PostgreSQL connection
        if not PSYCOPG2_AVAILABLE:
            raise RuntimeError("psycopg2-binary required. Install: pip install psycopg2-binary")
        
        conn = psycopg2.connect(config['url'], cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        return PostgreSQLConnection(conn)


class PostgreSQLConnection:
    """PostgreSQL connection wrapper compatible with SQLite interface."""
    
    def __init__(self, conn):
        self.conn = conn
        self.cursor = conn.cursor()
        self._in_explicit_transaction = False
    
    @property
    def in_transaction(self):
        return self._in_explicit_transaction or self.conn.status == psycopg2.extensions.TRANSACTION_STATUS_INTRANS
    
    def execute(self, sql, params=None):
        # Ignore PRAGMA
        if sql.strip().upper().startswith("PRAGMA"):
            return self
        
        # Handle transaction control
        sql_upper = sql.strip().upper()
        if sql_upper in ("BEGIN", "BEGIN IMMEDIATE"):
            if not self._in_explicit_transaction:
                self.conn.rollback()  # Clear state
                self._in_explicit_transaction = True
            return self
        
        if sql_upper == "COMMIT":
            self.conn.commit()
            self._in_explicit_transaction = False
            return self
        
        if sql_upper == "ROLLBACK":
            self.conn.rollback()
            self._in_explicit_transaction = False
            return self
        
        # Convert ? to %s
        pg_sql = sql.replace("?", "%s")
        
        # PostgreSQL type adjustments
        pg_sql = pg_sql.replace(" TEXT ", " VARCHAR ")
        pg_sql = pg_sql.replace(" INTEGER ", " BIGINT ")
        pg_sql = pg_sql.replace(" REAL ", " DOUBLE PRECISION ")
        
        # Execute
        if params:
            self.cursor.execute(pg_sql, params)
        else:
            self.cursor.execute(pg_sql)
        
        return self
    
    def executescript(self, script):
        # Remove PRAGMA, split and execute
        for statement in script.split(';'):
            statement = statement.strip()
            if statement and not statement.upper().startswith("PRAGMA"):
                self.execute(statement)
        return self
    
    def fetchone(self):
        try:
            row = self.cursor.fetchone()
            return dict(row) if row else None
        except psycopg2.ProgrammingError:
            return None
    
    def fetchall(self):
        try:
            rows = self.cursor.fetchall()
            return [dict(r) for r in rows] if rows else []
        except psycopg2.ProgrammingError:
            return []
    
    def close(self):
        try:
            if self._in_explicit_transaction:
                self.conn.rollback()
            self.cursor.close()
            self.conn.close()
        except:
            pass
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type and self._in_explicit_transaction:
            self.conn.rollback()
        self.close()
        return False
