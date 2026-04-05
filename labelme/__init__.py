import importlib.metadata
import os
import sys

__appname__ = "Labelme"

# Semantic Versioning 2.0.0: https://semver.org/
# 1. MAJOR version when you make incompatible API changes;
# 2. MINOR version when you add functionality in a backwards-compatible manner;
# 3. PATCH version when you make backwards-compatible bug fixes.
# e.g., 1.0.0a0, 1.0.0a1, 1.0.0b0, 1.0.0rc0, 1.0.0, 1.0.0.post0
__version__ = importlib.metadata.version("labelme")

# ---------------------------------------------------------------------------
# 模型文件目录与哈希缓存
#
# 策略：
#   程序维护一个"用户数据目录"，用于存放用户自行下载的模型文件和哈希缓存。
#   同时兼容项目内置的 models/blobs/ 目录（开发环境 / PyInstaller 打包预置模型）。
#
#   用户数据目录（跨平台）：
#     macOS  : ~/Library/Application Support/Labelme/
#     Windows: %APPDATA%\Labelme\
#     Linux  : $XDG_DATA_HOME/labelme/  (默认 ~/.local/share/labelme/)
#
#   用户只需将模型文件放入用户数据目录下的 models/ 子目录，
#   程序启动时会自动扫描并识别（通过 SHA-256 哈希，与文件名无关）。
#
#   哈希缓存文件（blob_hash_cache.json）保存在用户数据目录根部，
#   记录每个文件的 mtime、size 和 sha256，下次启动时只对新增/修改的文件
#   重新计算哈希，大幅减少启动时的 I/O 开销。
#
#   PyInstaller 打包后，_MEIPASS 目录只读，缓存统一写到用户数据目录，
#   不存在写入失败的问题。
# ---------------------------------------------------------------------------


def _get_user_data_dir() -> str:
    """返回跨平台的用户数据目录路径（不保证目录已存在）。"""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Labelme")
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(appdata, "Labelme")
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        return os.path.join(xdg, "labelme")


# 用户数据目录：存放用户自行下载的模型文件和哈希缓存
USER_DATA_DIR: str = _get_user_data_dir()
# 用户模型目录：用户将模型文件放在此处
USER_MODELS_DIR: str = os.path.join(USER_DATA_DIR, "models")
# 哈希缓存文件路径（始终写到用户数据目录，避免只读问题）
_CACHE_FILE: str = os.path.join(USER_DATA_DIR, "blob_hash_cache.json")

if getattr(sys, "frozen", False):
    # PyInstaller 打包后，资源文件解压到 sys._MEIPASS（只读）
    _BUILTIN_MODELS_DIR: str = os.path.join(
        sys._MEIPASS, "models", "blobs"  # type: ignore[attr-defined]
    )
else:
    # 开发环境：labelme/ 的上一级即项目根目录
    _PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _BUILTIN_MODELS_DIR = os.path.join(_PROJECT_ROOT, "models", "blobs")

# 需要扫描的目录列表（按优先级排列，用户目录优先）
_SCAN_DIRS: list[str] = [USER_MODELS_DIR, _BUILTIN_MODELS_DIR]

# 跳过的文件名（辅助文件，不是模型）
_SKIP_FILENAMES: frozenset[str] = frozenset(
    {".gitkeep", "README.md", "blob_hash_cache.json"}
)


def _load_cache(cache_file: str) -> dict:
    """从 JSON 缓存文件加载条目，返回 {相对键: {mtime, size, hash}} 字典。
    若文件不存在或格式错误则返回空字典。
    """
    import json

    try:
        with open(cache_file, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("version") == 1:
            return data.get("entries", {})
    except (OSError, ValueError, KeyError):
        pass
    return {}


def _save_cache(cache_file: str, entries: dict) -> None:
    """将条目写回 JSON 缓存文件。写入失败时静默忽略（如磁盘满、权限不足）。"""
    import json

    try:
        os.makedirs(os.path.dirname(cache_file), exist_ok=True)
        tmp_file = cache_file + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": entries}, f, indent=2)
        os.replace(tmp_file, cache_file)
    except OSError:
        pass


def _compute_sha256(filepath: str) -> str:
    """计算文件的 SHA-256 哈希，返回 'sha256:<hex>' 格式字符串。"""
    import hashlib

    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)
    return f"sha256:{sha256.hexdigest()}"


def _build_blob_hash_map(scan_dirs: list[str], cache_file: str) -> dict[str, str]:
    """扫描多个目录，利用缓存增量计算 SHA-256，
    返回 {"sha256:<hex>": "<绝对路径>"} 映射表。

    缓存键格式："{scan_dir_index}:{相对路径}"，以区分不同扫描目录中的同名文件。
    """
    # 加载现有缓存
    cached_entries: dict = _load_cache(cache_file)
    new_entries: dict = {}
    hash_map: dict[str, str] = {}
    cache_changed: bool = False

    for dir_index, scan_dir in enumerate(scan_dirs):
        if not os.path.isdir(scan_dir):
            continue

        for dirpath, _dirnames, filenames in os.walk(scan_dir):
            for filename in filenames:
                # 跳过辅助文件
                if filename.startswith(".") or filename in _SKIP_FILENAMES:
                    continue

                filepath = os.path.join(dirpath, filename)
                # 缓存键：目录索引 + 相对路径（跨平台统一用正斜杠）
                rel_path = os.path.relpath(filepath, scan_dir).replace(os.sep, "/")
                cache_key = f"{dir_index}:{rel_path}"

                try:
                    stat = os.stat(filepath)
                    mtime = stat.st_mtime
                    size = stat.st_size
                except OSError:
                    continue

                # 检查缓存是否命中（mtime 和 size 均一致）
                cached = cached_entries.get(cache_key)
                if (
                    cached
                    and cached.get("mtime") == mtime
                    and cached.get("size") == size
                ):
                    file_hash = cached["hash"]
                else:
                    # 缓存未命中，重新计算哈希
                    try:
                        file_hash = _compute_sha256(filepath)
                    except OSError:
                        continue
                    cache_changed = True

                new_entries[cache_key] = {
                    "mtime": mtime,
                    "size": size,
                    "hash": file_hash,
                }

                # 哈希映射：同一哈希若已存在则保留先扫描到的（用户目录优先）
                if file_hash not in hash_map:
                    hash_map[file_hash] = filepath

    # 检查是否有条目被删除
    if set(cached_entries.keys()) != set(new_entries.keys()):
        cache_changed = True

    # 若缓存有变化则写回
    if cache_changed:
        _save_cache(cache_file, new_entries)

    return hash_map


# 在模块加载时立即构建映射表（import labelme 时触发）
_BLOB_HASH_MAP: dict[str, str] = _build_blob_hash_map(
    scan_dirs=_SCAN_DIRS, cache_file=_CACHE_FILE
)

import osam.types._blob as _osam_blob_module  # noqa: E402

_original_blob_path_fget = _osam_blob_module.Blob.path.fget


def _patched_blob_path(self: _osam_blob_module.Blob) -> str:
    """优先从本地模型目录查找模型文件（通过哈希映射）；
    若未找到则回退到 osam 默认路径 ~/.cache/osam/models/blobs。
    """
    if self.hash in _BLOB_HASH_MAP:
        return _BLOB_HASH_MAP[self.hash]

    # 回退到 osam 默认路径
    return _original_blob_path_fget(self)  # type: ignore[return-value]


_osam_blob_module.Blob.path = property(_patched_blob_path)  # type: ignore[assignment]

# 同时 patch attachment 的路径查找：
# osam 在 pull() / size 等属性中通过 os.path.join(os.path.dirname(self.path), attachment.filename)
# 来定位附件，因此只要主文件路径正确，附件路径会自动正确。
# 但 attachment 本身的 .path 也会被调用（用于 size 检查），需要同样 patch。
# 由于 attachment 是普通 Blob（无 attachments），上面的逻辑已覆盖。

# ---------------------------------------------------------------------------

# XXX: has to be imported before PyQt5 to load dlls in order on Windows
# https://github.com/wkentaro/labelme/issues/1564
import onnxruntime

from labelme import utils
from labelme._label_file import LabelFile
