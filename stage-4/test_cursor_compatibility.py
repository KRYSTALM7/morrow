"""Test that both SQLite and PostgreSQL support row[0] and row["column"] access."""
import os
import tempfile
import pytest

def test_sqlite_cursor_compatibility():
    """Verify SQLite row access patterns."""
    from db_adapter import get_connection
    
    # Create temporary SQLite database
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    
    try:
        db = get_connection('test', path)
        
        # Create test table
        db.execute("CREATE TABLE test_table (id INTEGER, name TEXT)")
        db.execute("INSERT INTO test_table VALUES (1, 'Alice')")
        db.execute("INSERT INTO test_table VALUES (2, 'Bob')")
        
        # Test single row access
        row = db.execute("SELECT id, name FROM test_table WHERE id = 1").fetchone()
        
        # Test integer indexing (SQLite style)
        assert row[0] == 1, f"Expected row[0]=1, got {row[0]}"
        assert row[1] == 'Alice', f"Expected row[1]='Alice', got {row[1]}"
        
        # Test column name indexing (dict style)
        assert row["id"] == 1, f"Expected row['id']=1, got {row['id']}"
        assert row["name"] == 'Alice', f"Expected row['name']='Alice', got {row['name']}"
        
        # Test multiple rows
        rows = db.execute("SELECT id, name FROM test_table ORDER BY id").fetchall()
        assert len(rows) == 2
        
        # Test integer indexing on multiple rows
        assert rows[0][0] == 1
        assert rows[1][0] == 2
        
        # Test column name indexing on multiple rows
        assert rows[0]["name"] == 'Alice'
        assert rows[1]["name"] == 'Bob'
        
        # Test aggregate function with integer index
        count = db.execute("SELECT COUNT(*) FROM test_table").fetchone()[0]
        assert count == 2, f"Expected count=2, got {count}"
        
        db.close()
        print("✓ SQLite cursor compatibility verified")
        
    finally:
        os.unlink(path)


def test_postgresql_cursor_compatibility():
    """Verify PostgreSQL row access patterns (requires DATABASE_URL)."""
    database_url = os.environ.get("DATABASE_URL")
    
    if not database_url:
        pytest.skip("PostgreSQL test requires DATABASE_URL environment variable")
    
    from db_adapter import get_connection
    from db_config import get_database_config
    
    config = get_database_config()
    if config['type'] != 'postgresql':
        pytest.skip("DATABASE_URL not set or not PostgreSQL")
    
    db = get_connection('test', None)
    
    try:
        # Create test table
        db.execute("CREATE TABLE IF NOT EXISTS test_cursor_compat (id BIGINT, name VARCHAR(50))")
        db.execute("DELETE FROM test_cursor_compat")  # Clean up
        db.execute("INSERT INTO test_cursor_compat VALUES (1, 'Alice')")
        db.execute("INSERT INTO test_cursor_compat VALUES (2, 'Bob')")
        db.conn.commit()
        
        # Test single row access
        row = db.execute("SELECT id, name FROM test_cursor_compat WHERE id = 1").fetchone()
        
        # Test integer indexing (CRITICAL for existing code)
        assert row[0] == 1, f"Expected row[0]=1, got {row[0]}"
        assert row[1] == 'Alice', f"Expected row[1]='Alice', got {row[1]}"
        
        # Test column name indexing
        assert row["id"] == 1, f"Expected row['id']=1, got {row['id']}"
        assert row["name"] == 'Alice', f"Expected row['name']='Alice', got {row['name']}"
        
        # Test multiple rows
        rows = db.execute("SELECT id, name FROM test_cursor_compat ORDER BY id").fetchall()
        assert len(rows) == 2
        
        # Test integer indexing on multiple rows
        assert rows[0][0] == 1
        assert rows[1][0] == 2
        
        # Test column name indexing on multiple rows
        assert rows[0]["name"] == 'Alice'
        assert rows[1]["name"] == 'Bob'
        
        # Test aggregate function with integer index (CRITICAL)
        count = db.execute("SELECT COUNT(*) FROM test_cursor_compat").fetchone()[0]
        assert count == 2, f"Expected count=2, got {count}"
        
        # Clean up
        db.execute("DROP TABLE test_cursor_compat")
        db.conn.commit()
        db.close()
        
        print("✓ PostgreSQL cursor compatibility verified")
        
    except Exception as e:
        db.close()
        raise


if __name__ == "__main__":
    print("Testing cursor compatibility...")
    print()
    
    # Test SQLite
    try:
        test_sqlite_cursor_compatibility()
    except Exception as e:
        print(f"✗ SQLite test failed: {e}")
        raise
    
    # Test PostgreSQL if DATABASE_URL is set
    if os.environ.get("DATABASE_URL"):
        try:
            test_postgresql_cursor_compatibility()
        except Exception as e:
            print(f"✗ PostgreSQL test failed: {e}")
            raise
    else:
        print("⊘ PostgreSQL test skipped (DATABASE_URL not set)")
    
    print()
    print("✅ All cursor compatibility tests passed")
