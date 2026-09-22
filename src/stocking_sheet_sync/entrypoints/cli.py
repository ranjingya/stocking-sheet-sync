import argparse
import importlib
import sys

"""统一命令入口。"""


COMMANDS = {
    "serve": ("server", "启动Webhook和后台处理线程"),
    "rerun": ("rerun", "手动重新搬运并填充"),
    "inspect": ("inspect", "只读检查商品和历史销量"),
    "forecast": ("forecast", "只读试算老款公式预估"),
    "layout": ("layout", "只读预览市场部结构"),
    "layout-apply": ("layout_apply", "执行已核对的结构调整"),
    "sales-fill": ("sales_fill", "预览或执行历史销量填充"),
    "notify": ("notify", "手动发送结果通知"),
}


def run(argv: list[str] | None = None) -> int:
    """
    功能说明：解析统一子命令并交给对应入口，复用正式业务模块。

    参数：
        argv：命令行参数；省略时读取进程参数。
    返回值：对应子命令的退出码。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="下单需求表格填充", epilog="使用 子命令 --help 查看参数"
    )
    parser.add_argument(
        "command", choices=COMMANDS, help="；".join(k + "：" + v[1] for k, v in COMMANDS.items())
    )
    if not args or args[0] in ("-h", "--help"):
        parser.print_help()
        return 0
    parsed = parser.parse_args(args[:1])
    module = importlib.import_module(
        "stocking_sheet_sync.entrypoints." + COMMANDS[parsed.command][0]
    )
    return module.run(args[1:])


def main() -> None:
    raise SystemExit(run())
