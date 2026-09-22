"""单进程HTTP服务与后台消费线程的统一入口。"""

import argparse
import logging
import signal
from threading import Event, Thread

from waitress import create_server

from stocking_sheet_sync.entrypoints.web import create_app
from stocking_sheet_sync.infrastructure.queue import TaskQueue
from stocking_sheet_sync.logging import configure_logging
from stocking_sheet_sync.services.consumer import consume
from stocking_sheet_sync.settings import load_config

LOG = logging.getLogger(__name__)


def run(argv=None) -> int:
    """
    功能说明：在同一进程启动HTTP服务和串行消费线程，停止时等待当前任务。

    参数：
        argv：命令参数，支持绑定地址和端口。
    返回值：正常关闭为0，初始化异常向上抛出。
    """
    parser = argparse.ArgumentParser(description="启动Webhook和后台处理线程")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args(argv)
    config = load_config()
    configure_logging(config.log_level)
    stop = Event()
    queue = TaskQueue(config)
    server = None
    thread = None
    previous = {}

    def shutdown(*_):
        """停止接收新请求，并通知后台线程结束当前任务。"""
        if not stop.is_set():
            LOG.info("服务停止中，等待当前任务结束")
            stop.set()
            raise KeyboardInterrupt

    try:
        app = create_app(config, service=queue)
        server = create_server(app, host=args.host, port=args.port, threads=4)
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, shutdown)
        thread = Thread(target=consume, args=(config, stop), name="task-consumer", daemon=True)
        thread.start()
        LOG.info("服务启动：http://%s:%s，后台处理已开启", args.host, args.port)
        server.run()
    except KeyboardInterrupt:
        LOG.info("服务停止中，等待当前任务结束")
    finally:
        stop.set()
        try:
            if server is not None:
                server.close()
                server.task_dispatcher.shutdown(timeout=5)
        finally:
            if thread is not None and thread.ident is not None:
                thread.join(timeout=300)
                if thread.is_alive():
                    LOG.warning("停止等待超时，未确认任务将在下次启动恢复")
            queue.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return 0
