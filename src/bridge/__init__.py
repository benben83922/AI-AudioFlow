from src.bridge._recording import RecordingMixin
from src.bridge._processing import ProcessingMixin
from src.bridge._settings import SettingsMixin
from src.bridge._base import BridgeBase


class Bridge(
    RecordingMixin,
    ProcessingMixin,
    SettingsMixin,
    BridgeBase,
):
    """PyWebView Bridge — 所有暴露給 JS 的 API 方法集合。"""
