# 测试文件夹配置

当前本地 `config/config.toml` 的目标目录：

| 用途 | 飞书文件夹 | 配置字段 |
| --- | --- | --- |
| 原始备份、填充处理备份 | [备份](https://kocotree.feishu.cn/drive/folder/T9vzf7RRUlvgGBdMvmjcC1IPnnb) | `target.backup_folder_token` |
| 测试交付结果 | [交付-测试](https://kocotree.feishu.cn/drive/folder/TaT1fz5PMl5fjMdpWxGc5PN0nQh) | `target.folder_token` |

新批次使用上述目录。已有批次的目录随批次记录固定，重复触发复用原结果；需要按当前目录执行时，使用CLI强制重搬创建新批次。

实际配置文件由本机或宿主机管理，不纳入Git。Docker读取映射的 `/home/yatui/stocking-sheet-sync/config/config.toml`；本地配置修改不会自动同步服务器，运行中的进程需要重新加载配置后生效。

飞书文件夹元数据已通过服务端接口核对可读，未进行写入权限测试。当前流程保留三份在线副本。历史文件保留在各自原目录；本次配置调整不执行移动、删除或新建测试文件。导出及清理策略不属于此次目录配置变更。
