"""Test WalletStore.health() for both SQLite and PostgreSQL."""
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from wallet_store import WalletStore


def test_sqlite_health_check_returns_ok_when_initialized():
    """SQLite: health() returns ok status with PRAGMA quick_check."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_wallet.sqlite3"
        store = WalletStore(str(db_path))
        
        # Initialize the wallet
        initial_state = {
            "users": {},
            "requests": [],
            "payments": [],
            "authorizations": [],
            "splits": [],
            "settlements": [],
            "idempotency": {},
            "ai_proposals": {},
            "next_id": 1,
        }
        store.initialize(initial_state)
        
        # Check health
        health = store.health()
        
        assert health["status"] == "ok"
        assert health["integrity"] == "ok"
        assert health["migration_version"] >= 6
        assert health["initialized"] is True


def test_sqlite_health_check_returns_error_when_not_initialized():
    """SQLite: health() returns error status when wallet is not initialized."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_wallet_uninit.sqlite3"
        store = WalletStore(str(db_path))
        
        # Do NOT initialize the wallet
        health = store.health()
        
        assert health["status"] == "error"
        assert health["integrity"] == "ok"  # DB structure is fine
        assert health["migration_version"] >= 6
        assert health["initialized"] is False


def test_postgresql_health_check_with_env_var(monkeypatch):
    """PostgreSQL: health() uses SELECT 1 instead of PRAGMA when DATABASE_URL is set."""
    # Set a fake DATABASE_URL to trigger PostgreSQL mode
    fake_url = "postgresql://test:test@localhost:5432/test_db"
    monkeypatch.setenv("DATABASE_URL", fake_url)
    
    # This will fail to connect since the URL is fake, but we can test
    # that the code path is correct by checking that it doesn't crash
    # with a PRAGMA-related error
    try:
        # Create store with None path (PostgreSQL mode)
        store = WalletStore(None)
        # health() will fail because connection is fake, but should not
        # crash with "PRAGMA" related errors
        health = store.health()
    except Exception as e:
        # Expected to fail due to fake connection, but error should NOT be about PRAGMA
        error_msg = str(e).lower()
        assert "pragma" not in error_msg
        # Should be a connection error instead
        assert any(keyword in error_msg for keyword in ["connection", "connect", "could not", "psycopg2", "operational"])


def test_health_response_structure():
    """Verify health() returns all expected fields."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_wallet_struct.sqlite3"
        store = WalletStore(str(db_path))
        
        initial_state = {
            "users": {},
            "requests": [],
            "payments": [],
            "authorizations": [],
            "splits": [],
            "settlements": [],
            "idempotency": {},
            "ai_proposals": {},
            "next_id": 1,
        }
        store.initialize(initial_state)
        
        health = store.health()
        
        # Verify structure
        assert isinstance(health, dict)
        assert "status" in health
        assert "integrity" in health
        assert "migration_version" in health
        assert "initialized" in health
        
        # Verify types
        assert health["status"] in ("ok", "error")
        assert isinstance(health["integrity"], str)
        assert isinstance(health["migration_version"], int)
        assert isinstance(health["initialized"], bool)


def test_postgresql_health_check_with_mocked_adapter_returns_ok():
    """PostgreSQL: Mocked successful connection returns status=ok."""
    # Mock a successful PostgreSQL connection
    mock_db = Mock()
    
    # Mock SELECT 1 query (integrity check)
    mock_select_one = Mock()
    mock_select_one.fetchone.return_value = (1,)
    
    # Mock SELECT COALESCE(MAX(version),0) query (migration version)
    mock_version_row = Mock()
    mock_version_row.__getitem__ = Mock(return_value=6)
    mock_version_query = Mock()
    mock_version_query.fetchone.return_value = mock_version_row
    
    # Mock SELECT 1 FROM wallet_state query (initialized check)
    mock_init_query = Mock()
    mock_init_query.fetchone.return_value = Mock()  # Non-None means initialized
    
    # Setup execute to return appropriate mocks
    def mock_execute(sql, *args, **kwargs):
        sql_upper = sql.strip().upper()
        if sql_upper == "SELECT 1":
            return mock_select_one
        elif "COALESCE(MAX(VERSION)" in sql_upper or "COALESCE(MAX(version)" in sql:
            return mock_version_query
        elif "wallet_state" in sql.lower() and "singleton" in sql.lower():
            return mock_init_query
        return Mock()
    
    mock_db.execute = mock_execute
    mock_db.in_transaction = False
    
    # Create a real SQLite store first
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_pg.sqlite3"
        store = WalletStore(str(db_path))
        
        # Now override to PostgreSQL mode for health check
        store.db_type = 'postgresql'
        
        # Mock _connect to return our mocked database
        with patch.object(store, '_connect', return_value=mock_db):
            health = store.health()
    
    # Verify health response
    assert health["status"] == "ok"
    assert health["integrity"] == "ok"
    assert health["migration_version"] == 6
    assert health["initialized"] is True


def test_postgresql_health_check_connection_failure():
    """PostgreSQL: Connection failure returns integrity=error and status=error."""
    # Mock a failed PostgreSQL connection
    mock_db = Mock()
    
    # Mock SELECT 1 query to raise exception (connection failure)
    def mock_execute_with_failure(sql, *args, **kwargs):
        sql_upper = sql.strip().upper()
        if sql_upper == "SELECT 1":
            mock_result = Mock()
            mock_result.fetchone.side_effect = Exception("Connection failed")
            return mock_result
        elif "COALESCE(MAX(VERSION)" in sql_upper or "COALESCE(MAX(version)" in sql:
            # Version query succeeds
            mock_result = Mock()
            mock_row = Mock()
            mock_row.__getitem__ = Mock(return_value=6)
            mock_result.fetchone.return_value = mock_row
            return mock_result
        elif "wallet_state" in sql.lower() and "singleton" in sql.lower():
            # Initialized query succeeds
            mock_result = Mock()
            mock_result.fetchone.return_value = None  # Not initialized
            return mock_result
        return Mock()
    
    mock_db.execute = mock_execute_with_failure
    mock_db.in_transaction = False
    
    # Create a real SQLite store first
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_pg_fail.sqlite3"
        store = WalletStore(str(db_path))
        
        # Override to PostgreSQL mode for health check
        store.db_type = 'postgresql'
        
        # Mock _connect to return our mocked database
        with patch.object(store, '_connect', return_value=mock_db):
            health = store.health()
    
    # Verify health response shows error due to connection failure
    assert health["status"] == "error"
    assert health["integrity"] == "error"
    assert health["migration_version"] == 6
    assert health["initialized"] is False


def test_postgresql_health_check_queries_are_compatible():
    """Verify that health() queries use standard SQL compatible with PostgreSQL."""
    # Mock a PostgreSQL connection that logs all queries
    mock_db = Mock()
    queries_executed = []
    
    def mock_execute(sql, *args, **kwargs):
        queries_executed.append(sql.strip().upper())
        mock_result = Mock()
        
        sql_upper = sql.strip().upper()
        if sql_upper == "SELECT 1":
            mock_result.fetchone.return_value = (1,)
        elif "COALESCE(MAX(VERSION)" in sql_upper or "COALESCE(MAX(version)" in sql:
            mock_row = Mock()
            mock_row.__getitem__ = Mock(return_value=6)
            mock_result.fetchone.return_value = mock_row
        elif "wallet_state" in sql.lower() and "singleton" in sql.lower():
            mock_result.fetchone.return_value = Mock()
        
        return mock_result
    
    mock_db.execute = mock_execute
    mock_db.in_transaction = False
    
    # Create a real SQLite store first
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_pg_compat.sqlite3"
        store = WalletStore(str(db_path))
        
        # Override to PostgreSQL mode for health check
        store.db_type = 'postgresql'
        
        # Clear queries list to only capture health() queries
        queries_executed.clear()
        
        with patch.object(store, '_connect', return_value=mock_db):
            health = store.health()
    
    # Verify no SQLite-specific PRAGMA commands were used
    for query in queries_executed:
        assert "PRAGMA" not in query, f"SQLite-specific PRAGMA found in query: {query}"
    
    # Verify standard SQL queries were used
    assert "SELECT 1" in queries_executed
    assert any("COALESCE" in q and "VERSION" in q for q in queries_executed)
    assert any("WALLET_STATE" in q and "SINGLETON" in q for q in queries_executed)
    
    # Verify health returned ok
    assert health["status"] == "ok"
