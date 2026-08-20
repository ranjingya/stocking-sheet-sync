import logging


def configure_logging(level_name: str = "INFO") -> None:
    """
    功能说明：初始化应用日志，并在生产级别隐藏 HTTP 客户端请求明细。

    参数：
        level_name：应用日志级别名称；DEBUG 保留完整排障信息。

    返回值：无。
    """
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    dependency_level = logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    logging.getLogger("httpx").setLevel(dependency_level)
    logging.getLogger("httpcore").setLevel(dependency_level)
