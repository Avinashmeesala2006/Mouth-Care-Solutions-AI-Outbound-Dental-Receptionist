import os

# Tests never read the project .env (see config._should_ignore_env_file) and must not
# write call state into the project; use an in-memory SQLite call store.
os.environ.setdefault('CALL_STATE_DB_PATH', ':memory:')
