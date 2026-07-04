from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping

from nonebot.log import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageSequence


CANVAS_SIZE = (800, 800)
CONTENT_WIDTH = 720
CONTENT_SAFE_HEIGHT = 760
AVATAR_SIZE = 240
AVATAR_CACHE_MAXSIZE = 192
ANIMATED_AVATAR_CACHE_MAXSIZE = 24

# GIF 是最容易把渲染器拖慢或撑爆输出体积的格式；这里按 Plus 已验证参数收口。
GIF_MAX_FRAMES = 80
GIF_MIN_FRAME_DURATION_MS = 20
GIF_MAX_FRAME_DURATION_MS = 2000
GIF_FALLBACK_FRAME_DURATION_MS = 100

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
        from nonebot import get_plugin_config

        from .config import Config

        config = get_plugin_config(Config)
    except Exception as error:
        logger.debug(f"RollPig Pillow 字体配置读取失败，使用默认候选: {error}")
        return []

    configured_path = _resolve_font_path(config.rollpig_card_font_path)
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

    frame = ImageOps.exif_transpose(frame)
    frame = frame.convert("RGBA")
    return ImageOps.fit(
        frame,
        (AVATAR_SIZE, AVATAR_SIZE),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )


def _load_avatar(image_file: Path | None) -> Image.Image | None:
    """载入静态头像；缓存键包含 mtime/size，资源更新后会自动失效。"""

    if image_file is None:
        return None
    try:
        stat = image_file.stat()
    except OSError as error:
        logger.warning(f"RollPig 小猪图片状态读取失败，使用占位图: file={image_file}, error={error}")
        return None

    cached = _load_avatar_cached(str(image_file), stat.st_mtime_ns, stat.st_size)
    return cached.copy() if cached is not None else None


@lru_cache(maxsize=AVATAR_CACHE_MAXSIZE)
def _load_avatar_cached(path: str, mtime_ns: int, file_size: int) -> Image.Image | None:
    """读取并缩放头像资源；mtime/size 参数只用于构成 LRU 缓存失效键。"""

    image_file = Path(path)
    try:
        with Image.open(image_file) as opened:
            frame = opened.copy()
    except Exception as error:
        logger.warning(f"RollPig 小猪图片读取失败，使用占位图: file={image_file}, error={error}")
        return None

    return _fit_avatar_frame(frame)


def _normalize_gif_duration(raw_duration: object) -> int:
    """规整 GIF 帧间隔；防御 0ms/异常值让客户端播放过快或卡住。"""

    try:
        duration = int(raw_duration)
    except (TypeError, ValueError):
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    if duration <= 0:
        duration = GIF_FALLBACK_FRAME_DURATION_MS
    return min(max(duration, GIF_MIN_FRAME_DURATION_MS), GIF_MAX_FRAME_DURATION_MS)


def _load_animated_avatar_frames(image_file: Path | None) -> tuple[tuple[Image.Image, int], ...]:
    """载入动态头像帧；非 GIF 或单帧 GIF 返回空元组并交给静态 PNG 路径处理。"""

    if image_file is None or image_file.suffix.lower() != ".gif":
        return ()
    try:
        stat = image_file.stat()
    except OSError as error:
        logger.warning(f"RollPig GIF 图片状态读取失败，回退静态渲染: file={image_file}, error={error}")
        return ()

    cached = _load_animated_avatar_frames_cached(str(image_file), stat.st_mtime_ns, stat.st_size)
    return tuple((frame.copy(), duration) for frame, duration in cached)


@lru_cache(maxsize=ANIMATED_AVATAR_CACHE_MAXSIZE)
def _load_animated_avatar_frames_cached(path: str, mtime_ns: int, file_size: int) -> tuple[tuple[Image.Image, int], ...]:
    """读取 GIF 全帧并统一裁切；mtime/size 参数只用于构成 LRU 缓存失效键。"""

    image_file = Path(path)
    try:
        with Image.open(image_file) as opened:
            frame_count = int(getattr(opened, "n_frames", 1) or 1)
            if not getattr(opened, "is_animated", False) or frame_count <= 1:
                return ()
            if frame_count > GIF_MAX_FRAMES:
                logger.warning(f"RollPig GIF 帧数超过上限，已截断: file={image_file}, frames={frame_count}/{GIF_MAX_FRAMES}")

            frames: list[tuple[Image.Image, int]] = []
            for index, frame in enumerate(ImageSequence.Iterator(opened)):
                if index >= GIF_MAX_FRAMES:
                    break
                duration = _normalize_gif_duration(frame.info.get("duration", opened.info.get("duration")))
                frames.append((_fit_avatar_frame(frame.copy()), duration))
    except Exception as error:
        logger.warning(f"RollPig GIF 图片读取失败，回退静态渲染: file={image_file}, error={error}")
        return ()

    return tuple(frames)


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
) -> None:
    """水平居中绘制单行文本；原版不引入额外 Emoji 贴图库。"""

    if not text:
        return

    draw = ImageDraw.Draw(canvas)
    width = min(_measure_text(draw, text, font), max_width)
    x = (CANVAS_SIZE[0] - width) // 2
    bbox = draw.textbbox((0, 0), text, font=font)
    text_h = max(1, bbox[3] - bbox[1])
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
            )
            y += layout.analysis_line_height

    return _PreparedCard(canvas=canvas, layout=layout, avatar_y=start_y)


def _encode_png_card(prepared: _PreparedCard, image_file: Path | None) -> PigCardRenderResult:
    """把静态卡片编码为 PNG；静态 GIF 也会走这里取首帧。"""

    canvas = prepared.canvas.copy()
    _draw_avatar(canvas, _load_avatar(image_file), prepared.avatar_y)

    output = BytesIO()
    canvas.convert("RGB").save(output, format="PNG", optimize=True)
    return PigCardRenderResult(
        data=output.getvalue(),
        image_format="png",
        renderer="pillow",
        analysis_font_size=prepared.layout.analysis_font_size,
        analysis_lines=len(prepared.layout.analysis_lines),
    )


def _build_gif_palette(rgb_frames: list[Image.Image], avatar_y: int) -> Image.Image:
    """从静态文字层和全部头像帧采样调色板，避免彩色动画被第一帧压没。"""

    sample_size = AVATAR_SIZE
    palette_source = Image.new("RGB", (sample_size, sample_size * (len(rgb_frames) + 1)), BACKGROUND_COLOR[:3])

    full_card_sample = rgb_frames[0].resize((sample_size, sample_size), Image.Resampling.LANCZOS)
    palette_source.paste(full_card_sample, (0, 0))

    avatar_box = (
        (CANVAS_SIZE[0] - AVATAR_SIZE) // 2,
        avatar_y,
        (CANVAS_SIZE[0] + AVATAR_SIZE) // 2,
        avatar_y + AVATAR_SIZE,
    )
    for index, frame in enumerate(rgb_frames, 1):
        palette_source.paste(frame.crop(avatar_box), (0, sample_size * index))

    return palette_source.quantize(colors=256, method=Image.Quantize.MEDIANCUT)


def _encode_gif_card(prepared: _PreparedCard, avatar_frames: tuple[tuple[Image.Image, int], ...]) -> PigCardRenderResult:
    """把动态头像逐帧合成到静态卡片层，输出 GIF。"""

    rgb_frames: list[Image.Image] = []
    durations: list[int] = []
    for avatar, duration in avatar_frames:
        frame = prepared.canvas.copy()
        _draw_avatar(frame, avatar, prepared.avatar_y)
        rgb_frames.append(frame.convert("RGB"))
        durations.append(duration)

    palette = _build_gif_palette(rgb_frames, prepared.avatar_y)
    output_frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in rgb_frames]

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
    return PigCardRenderResult(
        data=output.getvalue(),
        image_format="gif",
        renderer="pillow-gif",
        analysis_font_size=prepared.layout.analysis_font_size,
        analysis_lines=len(prepared.layout.analysis_lines),
    )


# ================================ 对外入口 ================================ #


def _render_pig_card_image_sync(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
) -> PigCardRenderResult:
    """同步生成 800×800 卡片；动态 GIF 资源输出 GIF，其余输出 PNG。"""

    prepared = _prepare_card_without_avatar(pig_data)
    avatar_frames = _load_animated_avatar_frames(image_file)
    if avatar_frames:
        return _encode_gif_card(prepared, avatar_frames)
    return _encode_png_card(prepared, image_file)


async def render_pig_card_image(
    pig_data: Mapping[str, Any],
    image_file: Path | None,
) -> PigCardRenderResult:
    """异步入口：图片读取和编码放到线程中，避免阻塞 NoneBot 事件循环。"""

    return await asyncio.to_thread(_render_pig_card_image_sync, pig_data, image_file)
