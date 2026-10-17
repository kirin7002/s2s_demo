"""Server-only configuration. Never serialize this object to clients."""
from dataclasses import dataclass, field
import os
import re
from urllib.parse import urlencode


@dataclass(frozen=True)
class Config:
    api_key: str = field(default="", repr=False)
    demo_password: str = field(default="", repr=False)
    workspace: str = ""
    region: str = "cn-beijing"
    model: str = "qwen-audio-3.0-realtime-plus"
    voice: str = ""
    live_enabled: bool = False
    origin: str = "http://127.0.0.1:8080"
    max_sessions: int = 2
    max_seconds: float = 600
    handshake_seconds: float = 15
    idle_seconds: float = 60

    @classmethod
    def from_env(cls):
        return cls(api_key=os.getenv("DASHSCOPE_API_KEY", ""),
                   demo_password=os.getenv("S2S_DEMO_PASSWORD", ""),
                   workspace=os.getenv("QWEN_WORKSPACE_ID", ""),
                   region=os.getenv("QWEN_REGION", "cn-beijing"),
                   model=os.getenv("QWEN_MODEL", "qwen-audio-3.0-realtime-plus"),
                   voice=os.getenv("QWEN_VOICE", ""),
                   live_enabled=os.getenv("S2S_ENABLE_LIVE") == "1",
                   origin=os.getenv("S2S_ORIGIN", "http://127.0.0.1:8080"))

    def problem(self):
        if not self.live_enabled:
            return "真实模型连接未启用；本地页面可用。"
        if not self.demo_password:
            return "真实模型连接需要配置服务端演示登录口令。"
        if not self.api_key or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", self.workspace):
            return "服务端缺少有效 API Key 或业务空间 ID。"
        if self.region not in ("cn-beijing", "ap-southeast-1"):
            return "不支持的地域。"
        if self.model not in ("qwen-audio-3.1-realtime-plus", "qwen-audio-3.0-realtime-plus",
                              "qwen-audio-3.0-realtime-flash",
                              "qwen3.8-omni-flash-realtime", "qwen3.5-omni-plus-realtime",
                              "qwen3.5-omni-flash-realtime"):
            return "当前适配器不支持该型号，请选择已核对的 Audio 或 Omni Realtime 型号。"
        if len(self.voice) > 128:
            return "音色配置无效。"
        return None

    @property
    def url(self):
        return (f"wss://{self.workspace}.{self.region}.maas.aliyuncs.com/api-ws/v1/realtime?"
                + urlencode({"model": self.model}))

    def session_update(self):
        instructions = "你是车云一体语音对话演示助手。请用简洁自然的中文回答。当前未连接工具或持久记忆，不要声称执行了车辆操作或保存了记忆。"
        if self.is_audio:
            voice = self.voice or ("longanqian_v3.1" if "3.1" in self.model else "longanqian")
            return {"type": "session.update", "session": {
                "modalities": ["text", "audio"], "voice": voice,
                "input_audio_format": "pcm", "output_audio_format": "pcm",
                "turn_detection": {"type": "smart_turn"}, "instructions": instructions,
            }}
        return {"type": "session.update", "session": {
            "modalities": ["text", "audio"],
            "audio": {"input": {"format": {"type": "pcm", "sample_rate": 16000}},
                      "output": {"voice": self.voice or "Tina",
                                 "format": {"type": "pcm", "sample_rate": 24000}}},
            "turn_detection": {"type": "semantic_vad", "threshold": 0.5,
                               "silence_duration_ms": 800},
            "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
            "instructions": instructions,
        }}

    @property
    def is_audio(self):
        return self.model.startswith("qwen-audio-")
