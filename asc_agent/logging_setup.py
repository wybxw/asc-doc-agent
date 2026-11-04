"""日志降噪：只压掉与业务无关的传输层噪声，不吞真实错误。"""

from __future__ import annotations

import logging

# 这些 logger 的 INFO 是「每个 HTTP 往返打一行」，会把 agent 输出挤走。
NOISY_LOGGERS = (
    "httpx",     # 第三方库常用名
    "httpx2",    # mcp 2.x 实际依赖的名字（关键：只静音 httpx 不生效）
    "httpcore",
    "mcp",
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
)

# 收尾阶段（客户端会话关闭、server 关停）必然出现的连接重置。它不是缺陷，
# 但 asyncio 会以 ERROR 级别打整段 traceback，淹没 CLI 输出。
_BENIGN = ("_call_connection_lost", "ConnectionResetError", "ConnectionAbortedError")


class _DropBenignTransportNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.ERROR:
            return True
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 - 取不到消息就不要因为日志本身崩掉
            return True
        if "Exception in callback" in text and any(k in text for k in _BENIGN):
            return False
        if record.exc_info and any(k in repr(record.exc_info[1]) for k in _BENIGN):
            return False
        return True


def quiet_logs() -> None:
    """压掉传输层 INFO 与收尾期的连接重置 traceback。"""
    for name in NOISY_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        for handler in logger.handlers:
            handler.setLevel(logging.WARNING)

    # asyncio 的「Exception in callback」是 ERROR 级，需要靠 filter 精确拦截，
    # 不能整条降级——那会连真正的回调异常一起吞掉。
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.setLevel(logging.ERROR)
    if not any(isinstance(f, _DropBenignTransportNoise) for f in asyncio_logger.filters):
        asyncio_logger.addFilter(_DropBenignTransportNoise())

    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    for handler in root.handlers:
        handler.setLevel(logging.WARNING)
        if not any(isinstance(f, _DropBenignTransportNoise) for f in handler.filters):
            handler.addFilter(_DropBenignTransportNoise())
