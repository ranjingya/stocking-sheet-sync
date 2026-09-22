from __future__ import annotations

from urllib.parse import quote, urlsplit

from stocking_sheet_sync.domain.models import CopyResult

from .errors import CopyOutcomeUnknown, CopyRejected, FeishuApiError


class DriveOperations:
    """飞书云空间文件操作。"""

    def resolve_wiki_node(self, wiki_token: str) -> tuple[str, str, str]:
        """
        功能说明：将 Wiki 节点解析为其背后的真实云文档。

        参数：
            wiki_token：Wiki 节点 token。

        返回值：真实文档 token、文档类型和标题。
        """
        data = self._request(
            "GET",
            "/open-apis/wiki/v2/spaces/get_node",
            params={"token": wiki_token, "obj_type": "wiki"},
        )
        node = data.get("node")
        if not isinstance(node, dict):
            raise RuntimeError(f"Wiki 节点未返回真实文档信息：{wiki_token}")
        token = str(node.get("obj_token", "")).strip()
        document_type = str(node.get("obj_type", "")).strip()
        title = str(node.get("title", "")).strip() or "未命名表格"
        if not token or not document_type:
            raise RuntimeError(f"Wiki 节点未返回真实文档信息：{wiki_token}")
        return token, document_type, title

    def copy_spreadsheet(
        self, spreadsheet_token: str, copy_name: str, *, folder_token: str | None = None
    ) -> CopyResult:
        """
        功能说明：把源电子表格复制到配置的共享文件夹。

        参数：
            spreadsheet_token：源电子表格 token。
            copy_name：副本文件名。
            folder_token：可选目标文件夹；未指定时使用配置的交付文件夹。

        返回值：新副本的名称、token、类型和链接。
        """
        try:
            data = self._request(
                "POST",
                f"/open-apis/drive/v1/files/{quote(spreadsheet_token, safe='')}/copy",
                retry=False,
                json_body={
                    "folder_token": folder_token or self.config.target_folder_token,
                    "name": copy_name,
                    "type": "sheet",
                },
            )
            file_data = data.get("file")
            if not isinstance(file_data, dict):
                raise RuntimeError(f"复制接口未返回目标文件信息：{spreadsheet_token}")
            raw_token = file_data.get("token")
            raw_url = file_data.get("url")
            token = raw_token.strip() if isinstance(raw_token, str) else ""
            url = raw_url.strip() if isinstance(raw_url, str) else ""
            parsed_url = urlsplit(url)
            if not token or parsed_url.scheme != "https" or not parsed_url.netloc:
                raise RuntimeError(f"复制接口未返回目标文件信息：{spreadsheet_token}")
            return CopyResult(
                name=str(file_data.get("name", "")).strip() or copy_name,
                token=token,
                file_type=str(file_data.get("type", "sheet")),
                url=url,
            )
        except FeishuApiError as error:
            if (400 <= error.status < 500 and error.status not in {408, 499}) or (
                200 <= error.status < 300 and error.code > 0
            ):
                raise CopyRejected(str(error)) from error
            raise CopyOutcomeUnknown("复制结果不确定，请核对目标文件夹") from error
        except Exception as error:
            raise CopyOutcomeUnknown("复制结果不确定，请核对目标文件夹") from error

    def rename_spreadsheet(self, spreadsheet_token: str, title: str) -> None:
        """
        功能说明：通过服务端接口设置电子表格标题，并回读核验；标题相同时跳过写入。

        参数：
            spreadsheet_token：待改名的处理备份 token。
            title：完整目标标题。
        返回值：无；接口失败或回读不一致时抛错，可在同一批次中再次核对。
        """
        if not spreadsheet_token or not title.strip():
            raise ValueError("表格 token 和标题不能为空")
        path = f"/open-apis/sheets/v3/spreadsheets/{quote(spreadsheet_token, safe='')}"
        current = self._request("GET", path)["spreadsheet"]["title"]
        if current == title:
            self.logger.debug("备份标题已匹配：token=%s title=%s", spreadsheet_token, title)
            return
        self.logger.debug("更新备份标题：token=%s title=%s", spreadsheet_token, title)
        self._request("PATCH", path, retry=False, json_body={"title": title})
        actual = self._request("GET", path)["spreadsheet"]["title"]
        if actual != title:
            raise RuntimeError("备份标题回读不一致，请重试同一批次核对")
        self.logger.debug("备份标题核验完成：token=%s", spreadsheet_token)
