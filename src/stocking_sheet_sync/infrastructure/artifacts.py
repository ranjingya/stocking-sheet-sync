import logging
from pathlib import Path

"""管理本项目填充临时文件的数量和容量。"""


LOG = logging.getLogger(__name__)


def cleanup_temp_files(root: Path, max_files: int, max_bytes: int) -> None:
    """
    功能说明：按修改时间从旧到新删除临时文件，直到同时满足数量和容量上限。

    参数：
        root：专用于填充报告的临时目录，不跟随符号链接。
        max_files：保留的文件数量上限。
        max_bytes：保留文件总字节数上限。
    返回值：无；删除失败记录日志并继续处理其他旧文件。
    """
    if not root.exists() or root.is_symlink():
        return
    files = []
    for parent, dirs, names in root.walk(follow_symlinks=False):
        dirs[:] = [name for name in dirs if not (parent / name).is_symlink()]
        for name in names:
            path = parent / name
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            files.append((stat.st_mtime_ns, str(path), path, stat.st_size))
    count, size = len(files), sum(f[3] for f in files)
    LOG.debug("临时文件清理开始：files=%d bytes=%d", count, size)
    for _, _, path, length in sorted(files):
        if count <= max_files and size <= max_bytes:
            break
        try:
            path.unlink()
            count -= 1
            size -= length
            LOG.info("清理最老临时文件：path=%s bytes=%d", path, length)
        except OSError:
            LOG.warning("临时文件删除失败：path=%s", path, exc_info=True)
    LOG.debug("临时文件清理结束：files=%d bytes=%d", count, size)
