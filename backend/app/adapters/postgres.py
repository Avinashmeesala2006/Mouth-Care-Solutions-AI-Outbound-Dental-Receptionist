"""Optional live PostgreSQL health/transaction seam. Demo mode intentionally uses Repo."""
class PostgresRepository:
    def __init__(self, database_url: str): self.database_url=database_url
    def health(self):
        try:
            import psycopg
            with psycopg.connect(self.database_url, connect_timeout=3) as c: c.execute('SELECT 1')
            return True
        except Exception: return False
    def transaction(self):
        try:
            import psycopg
            return psycopg.connect(self.database_url)
        except ImportError as e: raise RuntimeError('install psycopg[binary] for live PostgreSQL') from e
