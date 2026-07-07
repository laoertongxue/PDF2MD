# src/parsing_core/parser/image_extractor.py
import base64
import re
from pathlib import Path

DATA_URI_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(data:(?P<mime>[\w/+]+);base64,(?P<data>[A-Za-z0-9+/=]+)\)"
)


def extract_images(markdown: str, images_dir: str) -> tuple[str, list[str]]:
    """将 MD 中所有 Base64 图片落盘，替换为本地路径。返回 (新 MD, 图片路径列表)。"""
    images: list[str] = []
    counter = 0

    def replace(match: re.Match) -> str:
        nonlocal counter
        alt = match.group("alt")
        mime = match.group("mime")
        data = match.group("data")
        ext = mime.split("/")[-1].split("+")[0]
        fname = f"img_{counter:03d}.{ext}"
        counter += 1
        fpath = Path(images_dir) / fname
        fpath.write_bytes(base64.b64decode(data))
        images.append(str(fpath))
        return f"![{alt}]({fpath})"

    new_md = DATA_URI_RE.sub(replace, markdown)
    return new_md, images
