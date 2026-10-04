"""Avoid SQLite's WAL-reset race on unpatched runtimes (sqlite.org/wal.html)."""
import sqlite3

def wal_is_safe(version=None):
    v=tuple(version or sqlite3.sqlite_version_info)
    return v>=(3,51,3) or (v[:2]==(3,50) and v[2]>=7) or (v[:2]==(3,44) and v[2]>=6)

def configure_journal(connection):
    mode='WAL' if wal_is_safe() else 'DELETE'
    actual=connection.execute('PRAGMA journal_mode='+mode).fetchone()[0].upper()
    if actual!=mode:raise RuntimeError('SQLite journal safety mode could not be established')
    return actual
