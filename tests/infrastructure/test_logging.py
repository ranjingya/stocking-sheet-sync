"""验证文件日志保留和重复初始化。"""

import subprocess
import sys


def test_logs_are_written_once_and_rotated_with_retention(tmp_path):
    script = '''
import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler
from stocking_sheet_sync.logging import configure_logging

path = Path(sys.argv[1])
configure_logging(log_dir=path)
configure_logging(log_dir=path)
root = logging.getLogger()
files = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
assert len(files) == 1
handler = files[0]
assert handler.maxBytes == 10 * 1024 * 1024
assert handler.backupCount == 5
logging.info("只记录一次")
handler.flush()
assert (path / "app.log").read_text().count("只记录一次") == 1
handler.maxBytes = 150
for n in range(20):
    logging.info("滚动测试-%d-%s", n, "内容" * 20)
handler.flush()
assert len(list(path.glob("app.log*"))) == 6
assert "滚动测试-19" in (path / "app.log").read_text()
'''
    subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=True, capture_output=True)
