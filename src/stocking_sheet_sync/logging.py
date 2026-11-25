"""统一控制台与滚动文件日志。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

SQL_ERROR_LOG = logging.getLogger("stocking_sheet_sync.sql_errors")
SQL_ERROR_LOG.propagate = False
SQL_ERROR_LOG.addHandler(logging.NullHandler())


def configure_logging(level_name: str = "INFO", *, log_dir: str | Path = "logs") -> None:
    """
    功能说明：输出控制台和滚动文件日志，数据库查询详情单独写文件。

    参数：
        level_name：日志级别，DEBUG包含详细排查信息。
        log_dir：日志保存目录，默认当前运行目录下的logs。
    返回值：无；文件无法创建时抛出异常。
    """
    level = getattr(logging, level_name, logging.INFO)
    directory = Path(log_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "app.log"
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s - %(message)s"
        if level <= logging.DEBUG
        else "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    owned = [handler for handler in root.handlers if getattr(handler, "_stocking_log", False)]
    if not any(
        isinstance(handler, RotatingFileHandler) and handler.baseFilename == str(destination)
        for handler in owned
    ):
        file_handler = RotatingFileHandler(
            destination, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        for handler in owned:
            root.removeHandler(handler)
            handler.close()
        owned = [logging.StreamHandler(), file_handler]
        for handler in owned:
            handler._stocking_log = True
            root.addHandler(handler)
    for handler in owned:
        handler.setLevel(level)
        handler.setFormatter(formatter)
    sql_destination = directory / "sql-errors.log"
    sql_handler = next(
        (h for h in SQL_ERROR_LOG.handlers if isinstance(h, RotatingFileHandler)), None
    )
    if sql_handler is None or sql_handler.baseFilename != str(sql_destination):
        if sql_handler is not None:
            SQL_ERROR_LOG.removeHandler(sql_handler)
            sql_handler.close()
        sql_handler = RotatingFileHandler(
            sql_destination, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        SQL_ERROR_LOG.addHandler(sql_handler)
    SQL_ERROR_LOG.setLevel(logging.WARNING)
    sql_handler.setFormatter(formatter)
    dependency_level = logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    logging.getLogger("httpx").setLevel(dependency_level)
    logging.getLogger("httpcore").setLevel(dependency_level)
