"""Database configuration supporting PostgreSQL (production) and SQLite (local)."""
import os
from urllib.parse import urlparse

def get_database_config():
    """Return database configuration based on DATABASE_URL or fallback to SQLite.
    
    Returns:
        dict: {
            'type': 'postgresql' or 'sqlite',
            'url': DATABASE_URL for PostgreSQL,
            'paths': {'wallet': ..., 'finance': ..., 'payments': ...} for SQLite
        }
    """
    database_url = os.environ.get("DATABASE_URL")
    
    if database_url:
        # PostgreSQL (Neon free tier or other)
        parsed = urlparse(database_url)
        if parsed.scheme not in ('postgres', 'postgresql'):
            raise ValueError(f"Invalid DATABASE_URL scheme: {parsed.scheme}")
        
        return {
            'type': 'postgresql',
            'url': database_url,
            'host': parsed.hostname,
            'port': parsed.port or 5432,
            'database': parsed.path.lstrip('/'),
            'user': parsed.username,
            'password': parsed.password,
        }
    else:
        # SQLite for local development and tests
        data_dir = os.environ.get("DATA_DIR", "data")
        os.makedirs(data_dir, exist_ok=True)
        
        return {
            'type': 'sqlite',
            'paths': {
                'wallet': os.path.join(data_dir, 'wallet.sqlite3'),
                'finance': os.path.join(data_dir, 'finance.sqlite3'),
                'payments': os.path.join(data_dir, 'payments.sqlite3'),
            }
        }

# Legacy compatibility
def get_data_dir():
    """Legacy: Get data directory for SQLite."""
    return os.environ.get("DATA_DIR", "data")

def get_db_paths():
    """Legacy: Get SQLite database paths."""
    config = get_database_config()
    if config['type'] == 'sqlite':
        return config['paths']
    else:
        # Fallback for mixed usage
        data_dir = get_data_dir()
        return {
            'wallet': os.path.join(data_dir, 'wallet.sqlite3'),
            'finance': os.path.join(data_dir, 'finance.sqlite3'),
            'payments': os.path.join(data_dir, 'payments.sqlite3'),
        }

def ensure_data_dir_exists():
    """Legacy: Create data directory for SQLite."""
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)

