import mimetypes
from typing import TYPE_CHECKING, Any, Literal, cast

from cookit.loguru import log_exception_warning, warning_suppress
from httpx import AsyncClient
from nonebot import logger
from nonebot.exception import NetworkError
from nonebot.matcher import current_bot, current_event
from nonebot_plugin_alconna.uniseg import Receipt, UniMessage

from ...config import config
from ...const import SONG_CACHE_DIR
from ...utils import encode_silk, ffmpeg_exists

if TYPE_CHECKING:
    from ...data_source import BaseSong, GeneralSongInfo


async def ensure_ffmpeg():
    if await ffmpeg_exists():
        return
    logger.warning(
        "FFmpeg 无法使用，插件将不会把音乐文件转为 silk 格式提交给协议端",
    )
    raise TypeError("FFmpeg unavailable, fallback to UniMessage")


def get_download_path(info: "GeneralSongInfo"):
    return SONG_CACHE_DIR / info.download_filename


async def download_song(info: "GeneralSongInfo"):
    file_path = get_download_path(info)
    if file_path.exists():
        return file_path

    async with AsyncClient(follow_redirects=True) as cli, cli.stream("GET", info.playable_url) as resp:  # fmt: skip
        resp.raise_for_status()
        SONG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with file_path.open("wb") as f:
            async for chunk in resp.aiter_bytes():
                f.write(chunk)
    return file_path


async def send_song_media_uni_msg(
    info: "GeneralSongInfo",
    raw: bool = False,
    as_file: bool = False,
):
    path = get_download_path(info)
    mime = t[0] if (t := mimetypes.guess_type(path.name)) else None
    kw_f = {"raw": path.read_bytes()} if raw else {"path": path}
    kw: Any = {**kw_f, "name": info.display_filename, "mimetype": mime}
    msg = UniMessage.file(**kw) if as_file else UniMessage.audio(**kw)
    return await msg.send(fallback=False)


async def send_song_voice_silk_uni_msg(info: "GeneralSongInfo"):
    await ensure_ffmpeg()
    return await UniMessage.voice(
        raw=(await encode_silk(get_download_path(info))).read_bytes(),
    ).send()


async def send_song_media_telegram(info: "GeneralSongInfo", as_file: bool = False):  # noqa: ARG001
    return await send_song_media_uni_msg(info, as_file=False)


async def _send_song_file_onebot_v11(info: "GeneralSongInfo"):
    from nonebot.adapters.onebot.v11 import (
        Bot as OB11Bot,
        GroupMessageEvent,
        PrivateMessageEvent,
    )

    bot = cast("OB11Bot", current_bot.get())
    event = current_event.get()

    if not isinstance(event, GroupMessageEvent | PrivateMessageEvent):
        raise TypeError("Event not supported")

    file = (
        str(get_download_path(info).resolve())
        if config.ob_v11_local_mode
        else cast(
            "str",
            (await bot.download_file(url=info.playable_url))["file"],
        )
    )

    if isinstance(event, PrivateMessageEvent):
        await bot.upload_private_file(
            user_id=event.user_id,
            file=file,
            name=info.display_filename,
        )
    else:
        await bot.upload_group_file(
            group_id=event.group_id,
            file=file,
            name=info.display_filename,
        )


async def send_song_media_onebot_v11(info: "GeneralSongInfo", as_file: bool = False):
    if as_file:
        try:
            return await _send_song_file_onebot_v11(info)
        except NetworkError as e:
            # maybe just upload timeout, we ignore it
            logger.info(f"Ignored NetworkError: {e}")
            return None
        except Exception as e:
            log_exception_warning(e, f"Send {info.father} as file failed")
            if config.ob_v11_ignore_send_file_failure:
                return None
            logger.warning("Falling back to voice message")

    return await send_song_voice_silk_uni_msg(info)


async def send_song_media_qq(info: "GeneralSongInfo", as_file: bool = False):  # noqa: ARG001
    return await send_song_voice_silk_uni_msg(info)


async def send_song_media_platform_specific(
    info: "GeneralSongInfo",
    as_file: bool = False,
) -> Receipt | None | Literal[False]:
    bot = current_bot.get()
    adapter_name = bot.adapter.get_name()
    processors = {
        "Telegram": send_song_media_telegram,
        "OneBot V11": send_song_media_onebot_v11,
        "QQ": send_song_media_qq,
    }
    if adapter_name not in processors:
        return False
    return await processors[adapter_name](info, as_file=as_file)


async def send_song_voice_with_card(song: "BaseSong"):
    """
    在发送音乐卡片后同步发送语音消息
    
    该函数用于在QQ平台环境下，当用户点歌成功后，除了发送音乐卡片外，
    还同步发送该歌曲的语音内容，提升用户体验。
    
    参数:
        song: 歌曲对象，包含歌曲信息和播放链接
        
    异常处理:
        - 网络波动导致下载失败时会记录警告日志
        - FFmpeg不可用时会抛出异常并由调用方处理
        - 语音编码失败时会记录详细错误信息
    """
    info = await song.get_info()
    try:
        await download_song(info)
    except Exception as e:
        log_exception_warning(e, f"Failed to download song {song} for voice")
        return None

    # 根据平台选择合适的发送方式
    bot = current_bot.get()
    adapter_name = bot.adapter.get_name()
    
    try:
        if adapter_name == "OneBot V11":
            # OneBot V11 使用原生语音消息
            return await _send_song_voice_onebot_v11(info)
        elif adapter_name == "QQ":
            # QQ 适配器使用 silk 格式语音
            return await send_song_voice_silk_uni_msg(info)
        else:
            # 其他平台使用通用语音消息
            return await send_song_voice_silk_uni_msg(info)
    except Exception as e:
        log_exception_warning(e, f"Failed to send voice for {song}")
        return None


async def _send_song_voice_onebot_v11(info: "GeneralSongInfo"):
    """
    使用 OneBot V11 协议发送语音消息
    
    将音乐文件转换为 silk 格式后，通过 OneBot V11 的 send_msg API 发送语音。
    支持群聊和私聊两种场景。
    
    参数:
        info: 歌曲信息对象
        
    返回:
        Receipt 对象或 None
    """
    from nonebot.adapters.onebot.v11 import (
        Bot as OB11Bot,
        GroupMessageEvent,
        MessageSegment,
        PrivateMessageEvent,
    )

    bot = cast("OB11Bot", current_bot.get())
    event = current_event.get()

    if not isinstance(event, GroupMessageEvent | PrivateMessageEvent):
        raise TypeError("Event not supported for voice message")

    # 确保 ffmpeg 可用并转换音频为 silk 格式
    await ensure_ffmpeg()
    silk_path = await encode_silk(get_download_path(info))
    
    # 读取 silk 文件内容
    voice_data = silk_path.read_bytes()
    
    # 构建语音消息段
    voice_segment = MessageSegment.record(file=voice_data)
    
    # 根据事件类型发送语音
    if isinstance(event, PrivateMessageEvent):
        return await bot.send_private_msg(
            user_id=event.user_id,
            message=voice_segment,
        )
    else:
        return await bot.send_group_msg(
            group_id=event.group_id,
            message=voice_segment,
        )


async def send_song_media(song: "BaseSong", as_file: bool | None = None):
    if as_file is None:
        as_file = config.send_as_file

    info = await song.get_info()
    try:
        await download_song(info)
    except Exception as e:
        log_exception_warning(e, f"Failed to download song {song}")
        return None

    with warning_suppress(f"Failed to send {song} using platform specific method"):
        r = await send_song_media_platform_specific(info, as_file=as_file)
        if (r is not False) or config.send_media_no_unimsg_fallback:
            return r or None
        logger.warning("Falling back to UniMessage")

    with warning_suppress(
        f"Failed to send {song} using file path, fallback using raw bytes",
    ):
        return await send_song_media_uni_msg(info, raw=False, as_file=as_file)
    return await send_song_media_uni_msg(info, raw=True, as_file=as_file)
