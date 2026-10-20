"""搬运前识别其他平台专供表。"""

from pathlib import Path
from urllib.parse import quote

from openpyxl.utils import get_column_letter

from stocking_sheet_sync.settings import business_view


def exclusive_source_reason(client, token: str, title: str, config_path: str) -> str:
    """
    功能说明：标题命中专供标记且全部工作表均无市场部表头时返回跳过原因。

    参数：
        client：仅用于读取源表结构和表头的飞书客户端。
        token：源电子表格标识。
        title：源文件标题。
        config_path：运行配置路径，用于读取同目录业务规则。
    返回值：应跳过时返回原因，否则返回空字符串；读取失败抛错，禁止误判。
    """
    rules = business_view(Path(config_path), "source_filter")
    keywords = rules.get("exclusive_title_keywords", [])
    if not isinstance(keywords, list) or any(
        not isinstance(k, str) or not k.strip() for k in keywords
    ):
        raise ValueError("专供标题关键词必须为非空字符串列表")

    def clean(value):
        """忽略标题与表头中的空白字符。"""
        return "".join(str(value or "").split())

    matched = next((k for k in keywords if clean(k) in clean(title)), None)
    if matched is None:
        return ""
    token = quote(token, safe="")
    sheets = client._request("GET", f"/open-apis/sheets/v3/spreadsheets/{token}/sheets/query")[
        "sheets"
    ]
    sheets = [s for s in sheets if s.get("resource_type", "sheet") == "sheet"]
    if not sheets:
        raise ValueError("源表没有可检查的工作表，无法判断专供范围")
    matching = rules["matching"]
    headers = {clean(h) for h in matching["market_headers"]}
    for sheet in sheets:
        grid = sheet["grid_properties"]
        end = get_column_letter(grid["column_count"])
        rows = min(grid["row_count"], matching["header_rows"])
        if rows < 1:
            raise ValueError("源表表头范围无效")
        area = f"{sheet['sheet_id']}!A1:{end}{rows}"
        data = client._request(
            "GET",
            f"/open-apis/sheets/v2/spreadsheets/{token}/values_batch_get",
            params={"ranges": area, "valueRenderOption": "UnformattedValue"},
        )["valueRanges"]
        if len(data) != 1 or data[0].get("range") != area or "values" not in data[0]:
            raise ValueError("源表表头读取不完整，无法判断专供范围")
        if any(clean(value) in headers for row in data[0]["values"] for value in row):
            return ""
    return f"其他平台专供表（{matched}，未发现市场部表头）"
