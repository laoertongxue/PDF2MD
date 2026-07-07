import shutil
import tempfile
from pathlib import Path


def snapshot(original_path: str) -> str:
    """生成原文件副本，永不触碰原文件。返回副本路径。"""
    suffix = Path(original_path).suffix
    snap = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    snap.close()
    shutil.copy2(original_path, snap.name)
    return snap.name
