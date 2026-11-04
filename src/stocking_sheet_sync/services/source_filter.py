"""搬运前识别其他平台专供表。"""

from pathlib import Path

from stocking_sheet_sync.settings import business_view


def exclusive_source_reason(title: str, config_path: str) -> str:
    """
    功能说明：标题同时包含配置的平台名和专供标记时返回跳过原因。

    参数：
        title：源文件标题。
        config_path：运行配置路径，用于读取同目录业务规则。
    返回值：应跳过时返回原因，否则返回空字符串；配置无效时抛错。
    """
    rules = business_view(Path(config_path), "source_filter")
    keywords = rules.get("exclusive_title_keywords", [])
    if not isinstance(keywords, list) or any(
        not isinstance(k, str) or not k.strip() for k in keywords
    ):
        raise ValueError("专供标题关键词必须为非空字符串列表")

    def clean(value):
        """忽略标题中的空白字符。"""
        return "".join(str(value or "").split())

    title = clean(title)
    matched = next(
        (k for k in keywords if "专供" in title and clean(k).removesuffix("专供") in title),
        None,
    )
    return f"其他平台专供表（{matched}）" if matched else ""
