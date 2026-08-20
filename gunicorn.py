from stocking_sheet_sync.config import load_config

runtime_log_level = load_config().log_level.lower()

bind = "0.0.0.0:5000"

workers = 1
worker_class = "gthread"
threads = 8

timeout = 300
graceful_timeout = 30
keepalive = 5

max_requests = 500
max_requests_jitter = 50

accesslog = "-" if runtime_log_level == "debug" else None
errorlog = "-"
loglevel = runtime_log_level
capture_output = True
