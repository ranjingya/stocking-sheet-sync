import logging


def configure_logging(level_name: str = "INFO") -> None:
    """功能说明：按配置级别初始化适合常驻服务查看的标准日志格式。"""
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
