from __future__ import annotations

import os
import re
import json
import asyncio
import hashlib
import threading
from io import BytesIO
from typing import Any
from pathlib import Path
from functools import lru_cache
from dataclasses import dataclass
from collections.abc import Mapping

from nonebot.log import logger
import nonebot_plugin_localstore as localstore
from PIL import Image, ImageOps, ImageDraw, ImageFont, ImageSequence

CANVAS_SIZE = (800, 800)
CONTENT_WIDTH = 720
CONTENT_SAFE_HEIGHT = 760
AVATAR_SIZE = 240

# GIF 只保留完整周期内的代表帧；源文件同时受像素工作量和绝对帧数约束。
GIF_TARGET_FRAMES = 60
GIF_MAX_DECODE_WORK_PIXELS = 16_000_000
GIF_ABSOLUTE_MAX_SOURCE_FRAMES = 600
GIF_MIN_FRAME_DURATION_MS = 20
GIF_MAX_FRAME_DURATION_MS = 2000
GIF_FALLBACK_FRAME_DURATION_MS = 100
GIF_PALETTE_SAMPLE_SIZE = 96
GIF_RENDER_CONCURRENCY = 2

# 原版卡片不含用户动态内容，适合直接缓存最终 PNG/GIF，避免常驻未压缩头像帧。
CARD_DISK_CACHE_MAX_BYTES = 64 * 1024 * 1024
CARD_CACHE_VERSION = 1
CARD_DISK_CACHE_MAGIC = b"ROLLPIG-CARD-CACHE-V1\n"
CARD_DISK_CACHE_HEADER_MAX_BYTES = 4096
CARD_CACHE_DIR = localstore.get_plugin_cache_dir() / "cards"

NAME_FONT_SIZE = 48
DESC_FONT_SIZE = 30
ANALYSIS_FONT_MAX_SIZE = 28
ANALYSIS_FONT_MIN_SIZE = 24

NAME_MARGIN_TOP = 20
DESC_MARGIN_TOP = 20
ANALYSIS_MARGIN_TOP = 30

NAME_LINE_HEIGHT = 58
DESC_LINE_HEIGHT = 38
ANALYSIS_LINE_HEIGHT_FACTOR = 1.5

BACKGROUND_COLOR = (255, 255, 255, 255)
NAME_COLOR = (32, 32, 32, 255)
DESC_COLOR = (85, 85, 85, 255)
ANALYSIS_COLOR = (51, 51, 51, 255)
PLACEHOLDER_BG = (255, 226, 239, 255)
PLACEHOLDER_FG = (154, 92, 135, 255)

PACKAGE_FONT_DIR = Path(__file__).parent / "resource" / "fonts"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:+#@%-]+|[^\S\n]+|\n|.", re.S)


@dataclass(frozen=True)
class PigCardRenderResult:
    """今日小猪卡片渲染结果；image_format 用于区分 PNG 与动态 GIF。"""

    data: bytes
    image_format: str
    renderer: str
    analysis_font_size: int
    analysis_lines: int


@dataclass(frozen=True)
class _TextLayout:
    name_line: str
    desc_line: str
    analysis_lines: list[str]
    analysis_font: ImageFont.ImageFont
    analysis_font_size: int
    analysis_line_height: int
    total_height: int


@dataclass(frozen=True)
class _PreparedCard:
    """不含头像的卡片底层；GIF 渲染时逐帧复用，避免重复排版和绘制文字。"""

    canvas: Image.Image
    layout: _TextLayout
    avatar_y: int


_card_cache_lock = threading.RLock()
_card_render_tasks: dict[tuple[object, ...], asyncio.Task[PigCardRenderResult]] = {}
_card_render_tasks_lock = asyncio.Lock()
_gif_render_semaphore: asyncio.Semaphore | None = None
_gif_render_semaphore_loop: asyncio.AbstractEventLoop | None = None
_gif_render_semaphore_guard = threading.Lock()


# ================================ 字体加载 ================================ #


def _resolve_font_path(value: str | None) -> Path | None:
    """解析用户配置的字体路径；相对路径按 Bot 工作目录处理，方便容器挂载。"""

    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    raw_path = Path(text).expanduser()
    return raw_path if raw_path.is_absolute() else Path.cwd() / raw_path


def _configured_font_candidates() -> list[Path]:
    """读取用户显式指定的字体；NoneBot 未初始化时静默跳过，方便离线测试脚本复用。"""

    try:
        from .config import plugin_config
    except Exception as error:
        logger.debug(f"RollPig Pillow 字体配置读取失败，使用默认候选: {error}")
        return []

    configured_path = _resolve_font_path(plugin_config.rollpig_card_font_path)
    return [configured_path] if configured_path is not None else []


def _font_candidates(*, bold: bool) -> list[Path]:
    """按优先级列出字体候选；原版只暴露一个字体配置项，避免配置面继续膨胀。"""

    packaged_fonts = [
        PACKAGE_FONT_DIR / "SourceHanSansSC-Medium.otf",
        PACKAGE_FONT_DIR / ("msyhbd.ttc" if bold else "msyh.ttc"),
        PACKAGE_FONT_DIR / "msyh.ttc",
        PACKAGE_FONT_DIR / "msyhbd.ttc",
    ]
    windows_fonts = [
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
        Path("C:/Windows/Fonts/simsun.ttc"),
    ]
    linux_fonts = [
        Path(
            "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf"
            if bold
            else "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf"
        ),
        Path("/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf"),
        Path(
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
            if bold
            else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
        ),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
    ]
    return [*_configured_font_candidates(), *packaged_fonts, *windows_fonts, *linux_fonts]


def _file_content_digest(file_path: Path) -> str:
    """流式计算内容摘要，避免同路径资源被替换后继续命中旧卡片。"""

    hasher = hashlib.sha256()
    with file_path.open("rb") as file_handle:
        while chunk := file_handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _first_font_file_signature(*, bold: bool) -> tuple[object, ...]:
    """记录实际使用字体；用户换字体后不得继续读取旧卡片成品。"""

    for font_path in _font_candidates(bold=bold):
        try:
            stat = font_path.stat()
            if font_path.is_file():
                return str(font_path.resolve()), stat.st_size, _file_content_digest(font_path)
        except OSError:
            continue
    return ("pillow-default",)


@lru_cache(maxsize=1)
def _card_render_asset_signature() -> tuple[object, ...]:
    """汇总跨重启稳定的字体指纹，作为磁盘缓存键的一部分。"""

    return _first_font_file_signature(bold=False), _first_font_file_signature(bold=True)


@lru_cache(maxsize=32)
def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    """加载指定字号字体；找不到中文字体时降级但不中断渲染。"""

    for font_path in _font_candidates(bold=bold):
        if not font_path.exists():
            continue
        try:
            return ImageFont.truetype(str(font_path), size=size)
        except Exception as error:
            logger.debug(f"RollPig Pillow 字体加载失败: path={font_path}, error={error}")

    logger.warning("RollPig Pillow 未找到可用中文字体，已退回 Pillow 默认字体。")
    return ImageFont.load_default()


# ================================ 文本测量与换行 ================================ #


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    """按实际像素测量文本宽度；Pillow 版本差异导致 textlength 失败时回退 bbox。"""

    if not text:
        return 0
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        bbox = draw.textbbox((0, 0), text, font=font)
        return max(0, bbox[2] - bbox[0])


def _font_line_top(font: ImageFont.ImageFont, y: int, line_height: int) -> int:
    """按字体整体指标定位行顶，避免每行内容 bbox 不同造成视觉行距漂移。"""

    try:
        ascent, descent = font.getmetrics()
        font_height = ascent + descent
    except Exception:
        # Pillow 默认字体等少数对象没有 getmetrics，用固定中英文样本估算稳定高度。
        mask = font.getmask("国Hg")
        bbox = mask.getbbox() or (0, 0, 1, line_height)
        font_height = max(1, bbox[3] - bbox[1])
    return int(y + max(0, line_height - font_height) / 2)


def _drop_last_text_unit(text: str) -> str:
    """删除最后一个显示单元；同时尽量避开常见 Emoji 变体符号的半截截断。"""

    if not text:
        return ""
    result = text[:-1]
    while result and result[-1] in ("\ufe0f", "\u200d"):
        result = result[:-1]
    return result


def _truncate_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    """把单行文本收敛到给定宽度，末尾使用中文省略号。"""

    if _measure_text(draw, text, font) <= max_width:
        return text

    ellipsis = "…"
    result = text
    while result and _measure_text(draw, f"{result}{ellipsis}", font) > max_width:
        result = _drop_last_text_unit(result)
    return f"{result}{ellipsis}" if result else ellipsis


def _append_ellipsis_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    """强制给截断行追加省略号，并保证追加后仍不超出最大宽度。"""

    ellipsis = "…"
    result = text.rstrip()
    while result and _measure_text(draw, f"{result}{ellipsis}", font) > max_width:
        result = _drop_last_text_unit(result)
    return f"{result}{ellipsis}" if result else ellipsis


def _wrap_text_by_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    *,
    max_lines: int | None = None,
) -> list[str]:
    """按像素宽度换行；中文逐字、英文数字成词，超过最大行数时省略。"""

    if not text:
        return []

    lines: list[str] = []
    current = ""

    def push_line(line: str) -> bool:
        lines.append(line.rstrip())
        return max_lines is not None and len(lines) >= max_lines

    for token in _TOKEN_RE.findall(text.replace("\r\n", "\n").replace("\r", "\n")):
        if token == "\n":
            if push_line(current):
                break
            current = ""
            continue

        candidate = f"{current}{token}"
        if current and _measure_text(draw, candidate.rstrip(), font) > max_width:
            if push_line(current):
                break
            current = token.lstrip()
            continue
        current = candidate
    else:
        if current or not lines:
            push_line(current)

    if max_lines is not None and len(lines) >= max_lines:
        consumed = "".join(lines)
        source_compact = text.replace("\n", "")
        if len(consumed) < len(source_compact):
            lines[-1] = _append_ellipsis_to_width(draw, lines[-1].rstrip(), font, max_width)

    return lines


# ================================ 图片与布局生成 ================================ #


def _make_canvas() -> Image.Image:
    """创建与原 HTML 模板一致的 800×800 白底画布。"""

    return Image.new("RGBA", CANVAS_SIZE, BACKGROUND_COLOR)


def _fit_avatar_frame(frame: Image.Image) -> Image.Image:
    """把任意来源图片统一规整为 240×240 RGBA 头像帧，对齐原模板 object-fit: cover。"""

    transposed = ImageOps.exif_transpose(frame)
    rgba_frame = transposed.convert("RGBA")
    try:
        return ImageOps.fit(
            rgba_frame,
            (AVATAR_SIZE, AVATAR_SIZE),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        )
    finally:
        if rgba_frame is not transposed:
            rgba_frame.close()
        if transposed is not frame:
            transposed.close()


def _load_avatar(image_file: Path | None) -> Image.Image | None:
    """载入静态头像；最终卡片由磁盘缓存复用，不常驻未压缩源图。"""

    if image_file is None:
        return None
    try:
        with Image.open(image_file) as opened:
            source_frame = opened.copy()
    except Exception as error:
        logger.warning(f"RollPig 小猪图片读取失败，使用占位图: file={image_file}, error={error}")
        return None

    try:
        return _fit_avatar_frame(source_frame)
    finally:
        source_frame.close()


def _normalize_gif_duration(raw_duration: object) -> int:
    """规整 GIF 帧间隔；防御 0ms/异常值让客户端播放过快或卡住。"""

    try:
        duration = int(raw_duration)
    except (TypeError, ValueError):
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    if duration <= 0:
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    return min(max(duration, GIF_MIN_FRAME_DURATION_MS), GIF_MAX_FRAME_DURATION_MS)


# ================================ 固定卡片磁盘缓存 ================================ #


def _card_text_values(pig_data: Mapping[str, Any]) -> tuple[str, str, str]:
    """统一实际绘制文案，保证缓存键与渲染输入完全一致。"""

    return (
        str(pig_data.get("name") or "未知小猪"),
        str(pig_data.get("description") or ""),
        str(pig_data.get("analysis") or "你今天是只神秘小猪。"),
    )


def _card_image_signature(image_file: Path | None) -> tuple[object, ...] | None:
    """生成源图指纹；文件正在切换时放弃缓存但仍允许即时渲染。"""

    if image_file is None:
        return ("image-missing",)
    try:
        stat = image_file.stat()
        if not image_file.is_file():
            return None
        return str(image_file.resolve()), stat.st_size, _file_content_digest(image_file)
    except OSError:
        return None


def _fixed_card_cache_key(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
) -> tuple[object, ...] | None:
    """生成固定卡片键；图片、文案、字体或渲染版本变化都会自动失效。"""

    image_signature = _card_image_signature(image_file)
    if image_signature is None:
        return None
    return (
        CARD_CACHE_VERSION,
        *image_signature,
        _card_render_asset_signature(),
        *_card_text_values(pig_data),
    )


def _fixed_card_cache_path(key: tuple[object, ...]) -> Path:
    """将完整输入摘要为稳定文件名，避免路径和中文直接进入缓存文件名。"""

    serialized = json.dumps(key, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return CARD_CACHE_DIR / f"v{CARD_CACHE_VERSION}-{digest}.cache"


def _serialize_card_disk_cache(result: PigCardRenderResult) -> bytes:
    """把成品图和排版指标写入单文件，避免图片与元数据不同步。"""

    header = json.dumps(
        {
            "image_format": result.image_format,
            "renderer": result.renderer,
            "analysis_font_size": result.analysis_font_size,
            "analysis_lines": result.analysis_lines,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return CARD_DISK_CACHE_MAGIC + len(header).to_bytes(4, "big") + header + result.data


def _deserialize_card_disk_cache(payload: bytes) -> PigCardRenderResult:
    """校验并读取本插件生成的 PNG/GIF 缓存容器。"""

    prefix_size = len(CARD_DISK_CACHE_MAGIC)
    if not payload.startswith(CARD_DISK_CACHE_MAGIC) or len(payload) < prefix_size + 4:
        raise ValueError("缓存魔数不匹配")

    header_size = int.from_bytes(payload[prefix_size : prefix_size + 4], "big")
    if not 0 < header_size <= CARD_DISK_CACHE_HEADER_MAX_BYTES:
        raise ValueError(f"缓存头长度非法: {header_size}")

    header_start = prefix_size + 4
    data_start = header_start + header_size
    if data_start >= len(payload):
        raise ValueError("缓存缺少图片数据")

    metadata = json.loads(payload[header_start:data_start].decode("utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("缓存头不是 object")
    image_data = payload[data_start:]
    if image_data.startswith((b"GIF87a", b"GIF89a")):
        detected_format = "gif"
    elif image_data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected_format = "png"
    else:
        raise ValueError("缓存正文不是 PNG 或 GIF")

    metadata_format = str(metadata.get("image_format") or detected_format).lower()
    if metadata_format != detected_format:
        raise ValueError(f"缓存格式不一致: metadata={metadata_format}, data={detected_format}")
    return PigCardRenderResult(
        data=image_data,
        image_format=detected_format,
        renderer=str(metadata.get("renderer") or f"pillow-{detected_format}"),
        analysis_font_size=int(metadata.get("analysis_font_size") or 0),
        analysis_lines=int(metadata.get("analysis_lines") or 0),
    )


def _remove_invalid_card_disk_cache(cache_file: Path, error: Exception) -> None:
    """删除单个损坏缓存；失败时仍会回退即时渲染。"""

    logger.warning(f"RollPig 卡片磁盘缓存损坏，已忽略: file={cache_file}, error={error}")
    try:
        cache_file.unlink(missing_ok=True)
    except OSError:
        pass


def _get_fixed_card(key: tuple[object, ...]) -> PigCardRenderResult | None:
    """读取固定卡片成品，并用 mtime 维护近似 LRU。"""

    cache_file = _fixed_card_cache_path(key)
    with _card_cache_lock:
        try:
            file_size = cache_file.stat().st_size
            if file_size > CARD_DISK_CACHE_MAX_BYTES:
                raise ValueError(f"缓存文件超过总容量: {file_size}")
            result = _deserialize_card_disk_cache(cache_file.read_bytes())
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
            _remove_invalid_card_disk_cache(cache_file, error)
            return None

        try:
            os.utime(cache_file, None)
        except OSError:
            # mtime 仅用于淘汰顺序；只读文件系统仍可读取已有缓存。
            pass
        return result


def _card_disk_cache_files() -> list[tuple[Path, int, int]]:
    """列出磁盘缓存路径、大小和 mtime，供容量淘汰与诊断使用。"""

    entries: list[tuple[Path, int, int]] = []
    try:
        for path in CARD_CACHE_DIR.glob("*.cache"):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((path, stat.st_size, stat.st_mtime_ns))
    except OSError:
        return []
    return entries


def _trim_card_disk_cache() -> None:
    """按最近使用时间将固定卡片缓存收敛到 64 MiB。"""

    entries = _card_disk_cache_files()
    total_bytes = sum(size for _, size, _ in entries)
    for path, size, _ in sorted(entries, key=lambda item: item[2]):
        if total_bytes <= CARD_DISK_CACHE_MAX_BYTES:
            break
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        total_bytes -= size


def _store_fixed_card(key: tuple[object, ...], result: PigCardRenderResult) -> bool:
    """原子写入最终成品；缓存不可写时不能影响本次发送。"""

    is_valid_gif = result.image_format == "gif" and result.data.startswith((b"GIF87a", b"GIF89a"))
    is_valid_png = result.image_format == "png" and result.data.startswith(b"\x89PNG\r\n\x1a\n")
    if not (is_valid_gif or is_valid_png):
        return False

    payload = _serialize_card_disk_cache(result)
    if len(payload) > CARD_DISK_CACHE_MAX_BYTES:
        logger.warning(
            "RollPig 卡片成品超过磁盘缓存总上限，本次不缓存: "
            f"bytes={len(payload)}/{CARD_DISK_CACHE_MAX_BYTES}"
        )
        return False

    cache_file = _fixed_card_cache_path(key)
    temporary_file = cache_file.with_name(f".{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with _card_cache_lock:
            CARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            temporary_file.write_bytes(payload)
            os.replace(temporary_file, cache_file)
            _trim_card_disk_cache()
    except OSError as error:
        logger.warning(f"RollPig 卡片磁盘缓存写入失败，本次继续发送即时结果: file={cache_file}, error={error}")
        try:
            temporary_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _get_gif_render_semaphore() -> asyncio.Semaphore:
    """按事件循环创建双并发锁，限制多个首次 GIF 生成峰值叠加。"""

    global _gif_render_semaphore, _gif_render_semaphore_loop

    loop = asyncio.get_running_loop()
    with _gif_render_semaphore_guard:
        if _gif_render_semaphore is None or _gif_render_semaphore_loop is not loop:
            _gif_render_semaphore = asyncio.Semaphore(GIF_RENDER_CONCURRENCY)
            _gif_render_semaphore_loop = loop
        return _gif_render_semaphore


def _gif_frame_groups(frame_count: int) -> tuple[tuple[int, int, int], ...]:
    """将完整动画均匀分组，返回每组起点、终点和代表帧。"""

    output_count = min(frame_count, GIF_TARGET_FRAMES)
    groups: list[tuple[int, int, int]] = []
    for output_index in range(output_count):
        start = output_index * frame_count // output_count
        end = (output_index + 1) * frame_count // output_count
        groups.append((start, end, (start + end) // 2))
    return tuple(groups)


def _load_animated_avatar_frames(image_file: Path | None) -> tuple[tuple[Image.Image, int], ...]:
    """按工作量预算读取 GIF，只保留完整周期内均匀采样后的头像帧。"""

    if image_file is None or image_file.suffix.lower() != ".gif":
        return ()
    decoded: list[Image.Image] = []
    try:
        with Image.open(image_file) as opened:
            frame_count = int(getattr(opened, "n_frames", 1) or 1)
            if not getattr(opened, "is_animated", False) or frame_count <= 1:
                return ()
            width, height = opened.size
            decode_work_pixels = width * height * frame_count
            if frame_count > GIF_ABSOLUTE_MAX_SOURCE_FRAMES:
                logger.warning(
                    "RollPig GIF 超过绝对源帧上限，已降级为静态首帧: "
                    f"file={image_file}, frames={frame_count}/{GIF_ABSOLUTE_MAX_SOURCE_FRAMES}"
                )
                return ()
            if decode_work_pixels > GIF_MAX_DECODE_WORK_PIXELS:
                logger.warning(
                    "RollPig GIF 超过解码工作量预算，已降级为静态首帧: "
                    f"file={image_file}, size={width}x{height}, frames={frame_count}, "
                    f"pixel_frames={decode_work_pixels}/{GIF_MAX_DECODE_WORK_PIXELS}"
                )
                return ()

            groups = _gif_frame_groups(frame_count)
            durations = [0] * len(groups)
            group_index = 0
            for index, frame in enumerate(ImageSequence.Iterator(opened)):
                while group_index + 1 < len(groups) and index >= groups[group_index][1]:
                    group_index += 1
                duration = _normalize_gif_duration(frame.info.get("duration", opened.info.get("duration")))
                durations[group_index] += duration
                if index != groups[group_index][2]:
                    continue

                source_frame = frame.copy()
                try:
                    decoded.append(_fit_avatar_frame(source_frame))
                finally:
                    source_frame.close()

            if len(decoded) != len(groups):
                raise ValueError(f"GIF 抽样帧数量异常: decoded={len(decoded)}, expected={len(groups)}")
            if frame_count > len(groups):
                logger.info(
                    f"RollPig GIF 已在完整周期内均匀抽帧: "
                    f"file={image_file}, frames={frame_count}->{len(groups)}"
                )
            return tuple(zip(decoded, durations))
    except Exception as error:
        for frame in decoded:
            frame.close()
        logger.warning(f"RollPig GIF 图片读取失败，回退静态渲染: file={image_file}, error={error}")
        return ()


def _draw_avatar(canvas: Image.Image, avatar: Image.Image | None, y: int) -> None:
    """绘制头像；保持原 HTML 的方形 240×240，不额外做圆角和阴影。"""

    x = (CANVAS_SIZE[0] - AVATAR_SIZE) // 2
    if avatar is not None:
        canvas.alpha_composite(avatar, (x, y))
        return

    draw = ImageDraw.Draw(canvas)
    draw.rectangle((x, y, x + AVATAR_SIZE, y + AVATAR_SIZE), fill=PLACEHOLDER_BG)
    placeholder_font = _load_font(86, bold=True)
    _draw_text_line(
        canvas,
        "猪",
        placeholder_font,
        y + (AVATAR_SIZE - 100) // 2,
        100,
        PLACEHOLDER_FG,
        max_width=AVATAR_SIZE,
    )


def _build_text_layout(
    draw: ImageDraw.ImageDraw,
    *,
    name: str,
    desc: str,
    analysis: str,
) -> _TextLayout:
    """计算普通卡片排版；分析正文优先缩字号，最后才省略。"""

    name_font = _load_font(NAME_FONT_SIZE, bold=True)
    desc_font = _load_font(DESC_FONT_SIZE)

    name_line = _truncate_to_width(draw, name, name_font, CONTENT_WIDTH)
    desc_line = _truncate_to_width(draw, desc, desc_font, CONTENT_WIDTH) if desc else ""

    static_height = AVATAR_SIZE + NAME_MARGIN_TOP + NAME_LINE_HEIGHT
    if desc_line:
        static_height += DESC_MARGIN_TOP + DESC_LINE_HEIGHT
    if analysis:
        static_height += ANALYSIS_MARGIN_TOP

    for analysis_font_size in range(ANALYSIS_FONT_MAX_SIZE, ANALYSIS_FONT_MIN_SIZE - 1, -1):
        analysis_font = _load_font(analysis_font_size)
        analysis_line_height = round(analysis_font_size * ANALYSIS_LINE_HEIGHT_FACTOR)
        analysis_lines = _wrap_text_by_width(draw, analysis, analysis_font, CONTENT_WIDTH) if analysis else []
        total_height = static_height + len(analysis_lines) * analysis_line_height
        if total_height <= CONTENT_SAFE_HEIGHT:
            return _TextLayout(
                name_line=name_line,
                desc_line=desc_line,
                analysis_lines=analysis_lines,
                analysis_font=analysis_font,
                analysis_font_size=analysis_font_size,
                analysis_line_height=analysis_line_height,
                total_height=total_height,
            )

    analysis_font = _load_font(ANALYSIS_FONT_MIN_SIZE)
    analysis_line_height = round(ANALYSIS_FONT_MIN_SIZE * ANALYSIS_LINE_HEIGHT_FACTOR)
    available_height = max(analysis_line_height, CONTENT_SAFE_HEIGHT - static_height)
    max_lines = max(1, available_height // analysis_line_height)
    analysis_lines = (
        _wrap_text_by_width(draw, analysis, analysis_font, CONTENT_WIDTH, max_lines=max_lines)
        if analysis
        else []
    )
    total_height = static_height + len(analysis_lines) * analysis_line_height
    return _TextLayout(
        name_line=name_line,
        desc_line=desc_line,
        analysis_lines=analysis_lines,
        analysis_font=analysis_font,
        analysis_font_size=ANALYSIS_FONT_MIN_SIZE,
        analysis_line_height=analysis_line_height,
        total_height=total_height,
    )


# ================================ 绘制与编码 ================================ #


def _draw_text_line(
    canvas: Image.Image,
    text: str,
    font: ImageFont.ImageFont,
    y: int,
    line_height: int,
    fill: tuple[int, int, int, int],
    *,
    max_width: int,
    align_by_baseline: bool = False,
) -> None:
    """水平居中绘制单行文本；正文可按固定基线避免混合字符上下漂移。"""

    if not text:
        return

    draw = ImageDraw.Draw(canvas)
    width = min(_measure_text(draw, text, font), max_width)
    x = (CANVAS_SIZE[0] - width) // 2
    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = max(1, bbox[3] - bbox[1])
    if align_by_baseline:
        text_y = _font_line_top(font, y, line_height)
    else:
        text_y = int(y + (line_height - text_h) / 2 - bbox[1])
    draw.text((x, text_y), text, fill=fill, font=font)


def _prepare_card_without_avatar(pig_data: Mapping[str, Any]) -> _PreparedCard:
    """生成不含头像的静态卡片层；动态头像逐帧贴在同一位置。"""

    name = str(pig_data.get("name") or "未知小猪")
    desc = str(pig_data.get("description") or "")
    analysis = str(pig_data.get("analysis") or "你今天是只神秘小猪。")

    canvas = _make_canvas()
    draw = ImageDraw.Draw(canvas)
    layout = _build_text_layout(draw, name=name, desc=desc, analysis=analysis)

    start_y = max(20, (CANVAS_SIZE[1] - layout.total_height) // 2)
    y = start_y + AVATAR_SIZE + NAME_MARGIN_TOP

    name_font = _load_font(NAME_FONT_SIZE, bold=True)
    _draw_text_line(canvas, layout.name_line, name_font, y, NAME_LINE_HEIGHT, NAME_COLOR, max_width=CONTENT_WIDTH)
    y += NAME_LINE_HEIGHT

    if layout.desc_line:
        y += DESC_MARGIN_TOP
        desc_font = _load_font(DESC_FONT_SIZE)
        _draw_text_line(canvas, layout.desc_line, desc_font, y, DESC_LINE_HEIGHT, DESC_COLOR, max_width=CONTENT_WIDTH)
        y += DESC_LINE_HEIGHT

    if layout.analysis_lines:
        y += ANALYSIS_MARGIN_TOP
        for line in layout.analysis_lines:
            _draw_text_line(
                canvas,
                line,
                layout.analysis_font,
                y,
                layout.analysis_line_height,
                ANALYSIS_COLOR,
                max_width=CONTENT_WIDTH,
                align_by_baseline=True,
            )
            y += layout.analysis_line_height

    return _PreparedCard(canvas=canvas, layout=layout, avatar_y=start_y)


def _encode_png_card(prepared: _PreparedCard, image_file: Path | None) -> PigCardRenderResult:
    """把静态卡片编码为 PNG；静态 GIF 也会走这里取首帧。"""

    canvas = prepared.canvas.copy()
    avatar = _load_avatar(image_file)
    try:
        _draw_avatar(canvas, avatar, prepared.avatar_y)
        rgb_canvas = canvas.convert("RGB")
        try:
            output = BytesIO()
            rgb_canvas.save(output, format="PNG", optimize=True)
        finally:
            rgb_canvas.close()
    finally:
        if avatar is not None:
            avatar.close()
        canvas.close()
    return PigCardRenderResult(
        data=output.getvalue(),
        image_format="png",
        renderer="pillow",
        analysis_font_size=prepared.layout.analysis_font_size,
        analysis_lines=len(prepared.layout.analysis_lines),
    )


def _build_gif_palette(
    prepared: _PreparedCard,
    avatar_frames: tuple[tuple[Image.Image, int], ...],
) -> Image.Image:
    """用小尺寸样本构造全局调色板，不常驻整批 800×800 RGB 帧。"""

    sample_size = GIF_PALETTE_SAMPLE_SIZE
    palette_source = Image.new(
        "RGB",
        (sample_size, sample_size * (len(avatar_frames) + 1)),
        BACKGROUND_COLOR[:3],
    )

    static_rgb = prepared.canvas.convert("RGB")
    try:
        full_card_sample = static_rgb.resize((sample_size, sample_size), Image.Resampling.LANCZOS)
        try:
            palette_source.paste(full_card_sample, (0, 0))
        finally:
            full_card_sample.close()
    finally:
        static_rgb.close()

    avatar_box = (
        (CANVAS_SIZE[0] - AVATAR_SIZE) // 2,
        prepared.avatar_y,
        (CANVAS_SIZE[0] + AVATAR_SIZE) // 2,
        prepared.avatar_y + AVATAR_SIZE,
    )
    avatar_background = prepared.canvas.crop(avatar_box)
    try:
        for index, (avatar, _) in enumerate(avatar_frames, 1):
            sample = avatar_background.copy()
            try:
                sample.alpha_composite(avatar, (0, 0))
                sample_rgb = sample.convert("RGB")
            finally:
                sample.close()
            try:
                resized = sample_rgb.resize((sample_size, sample_size), Image.Resampling.LANCZOS)
            finally:
                sample_rgb.close()
            try:
                palette_source.paste(resized, (0, sample_size * index))
            finally:
                resized.close()
    finally:
        avatar_background.close()

    try:
        return palette_source.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    finally:
        palette_source.close()


def _encode_gif_card(
    prepared: _PreparedCard,
    avatar_frames: tuple[tuple[Image.Image, int], ...],
) -> PigCardRenderResult:
    """逐帧合成并立即量化，仅保留单字节索引帧以压低内存峰值。"""

    palette = _build_gif_palette(prepared, avatar_frames)
    output_frames: list[Image.Image] = []
    durations: list[int] = []
    try:
        for avatar, duration in avatar_frames:
            frame = prepared.canvas.copy()
            try:
                _draw_avatar(frame, avatar, prepared.avatar_y)
                rgb_frame = frame.convert("RGB")
                try:
                    output_frames.append(rgb_frame.quantize(palette=palette, dither=Image.Dither.NONE))
                finally:
                    rgb_frame.close()
            finally:
                frame.close()
            durations.append(duration)

        output = BytesIO()
        output_frames[0].save(
            output,
            format="GIF",
            save_all=True,
            append_images=output_frames[1:],
            duration=durations,
            loop=0,
            disposal=2,
            optimize=False,
        )
        result_data = output.getvalue()
    finally:
        palette.close()
        for frame in output_frames:
            frame.close()
    return PigCardRenderResult(
        data=result_data,
        image_format="gif",
        renderer="pillow-gif",
        analysis_font_size=prepared.layout.analysis_font_size,
        analysis_lines=len(prepared.layout.analysis_lines),
    )


# ================================ 对外入口 ================================ #


def _render_pig_card_image_sync(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    _final_cache_key: tuple[object, ...] | None = None,
) -> PigCardRenderResult:
    """同步生成卡片；固定输入缓存最终成品，动态 GIF 不保留未压缩源帧。"""

    final_cache_key = _final_cache_key or _fixed_card_cache_key(pig_data, image_file)
    if final_cache_key is not None:
        cached = _get_fixed_card(final_cache_key)
        if cached is not None:
            return cached

    prepared = _prepare_card_without_avatar(pig_data)
    avatar_frames: tuple[tuple[Image.Image, int], ...] = ()
    try:
        avatar_frames = _load_animated_avatar_frames(image_file)
        result = (
            _encode_gif_card(prepared, avatar_frames)
            if avatar_frames
            else _encode_png_card(prepared, image_file)
        )
    finally:
        prepared.canvas.close()
        for frame, _ in avatar_frames:
            frame.close()

    if final_cache_key is not None:
        # 资源同步可能在渲染期间替换 active；只有源图指纹仍一致时才写回旧键。
        current_cache_key = _fixed_card_cache_key(pig_data, image_file)
        if current_cache_key == final_cache_key:
            _store_fixed_card(final_cache_key, result)
    return result


def _read_fixed_card_cache(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
) -> tuple[tuple[object, ...] | None, PigCardRenderResult | None]:
    """在线程中计算缓存键并读取成品，避免哈希和磁盘 IO 阻塞事件循环。"""

    cache_key = _fixed_card_cache_key(pig_data, image_file)
    cached = _get_fixed_card(cache_key) if cache_key is not None else None
    return cache_key, cached


async def render_pig_card_image(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
) -> PigCardRenderResult:
    """异步入口：固定卡片并发合流，首次 GIF 受全局双并发限制。"""

    final_cache_key, cached = await asyncio.to_thread(_read_fixed_card_cache, pig_data, image_file)
    if cached is not None:
        return cached

    if final_cache_key is not None:
        task_key = ("fixed-card", *final_cache_key)
        async with _card_render_tasks_lock:
            render_task = _card_render_tasks.get(task_key)
            if render_task is None:
                render_task = asyncio.create_task(
                    _render_card_once(task_key, pig_data, image_file, final_cache_key=final_cache_key)
                )
                _card_render_tasks[task_key] = render_task
        # 一个调用方取消时不能连带取消其他等待同一卡片的请求。
        return await asyncio.shield(render_task)

    if image_file is not None and image_file.suffix.lower() == ".gif":
        async with _get_gif_render_semaphore():
            return await asyncio.to_thread(_render_pig_card_image_sync, pig_data, image_file)
    return await asyncio.to_thread(_render_pig_card_image_sync, pig_data, image_file)


async def _render_card_once(
    task_key: tuple[object, ...],
    pig_data: Mapping[str, Any],
    image_file: Path | None,
    *,
    final_cache_key: tuple[object, ...],
) -> PigCardRenderResult:
    """执行一次共享渲染；GIF 进入双并发预算，结束后移除在途任务。"""

    try:
        if image_file is not None and image_file.suffix.lower() == ".gif":
            async with _get_gif_render_semaphore():
                return await asyncio.to_thread(
                    _render_pig_card_image_sync,
                    pig_data,
                    image_file,
                    _final_cache_key=final_cache_key,
                )
        return await asyncio.to_thread(
            _render_pig_card_image_sync,
            pig_data,
            image_file,
            _final_cache_key=final_cache_key,
        )
    finally:
        current_task = asyncio.current_task()
        async with _card_render_tasks_lock:
            if _card_render_tasks.get(task_key) is current_task:
                _card_render_tasks.pop(task_key, None)


def get_card_renderer_cache_stats() -> dict[str, int]:
    """返回固定卡片磁盘占用，供运行诊断和性能回归使用。"""

    entries = _card_disk_cache_files()
    return {
        "final_entries": len(entries),
        "final_bytes": sum(size for _, size, _ in entries),
    }


def clear_card_renderer_caches() -> None:
    """释放进程内字体和并发对象；磁盘成品跨重启保留。"""

    global _gif_render_semaphore, _gif_render_semaphore_loop

    _load_font.cache_clear()
    _card_render_asset_signature.cache_clear()
    with _gif_render_semaphore_guard:
        _gif_render_semaphore = None
        _gif_render_semaphore_loop = None


async def shutdown_card_renderer() -> None:
    """等待在途卡片结束后释放进程内渲染资源。"""

    async with _card_render_tasks_lock:
        render_tasks = list(_card_render_tasks.values())
    if render_tasks:
        await asyncio.gather(*render_tasks, return_exceptions=True)
    clear_card_renderer_caches()
