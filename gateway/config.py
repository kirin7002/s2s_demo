"""Server-only configuration. Never serialize this object to clients."""
from dataclasses import dataclass, field
import os
import math
import re
from urllib.parse import urlencode
from .envfile import DEFAULT_ENV_FILE, read_env_file
from .target_interface import voice_target_tool


@dataclass(frozen=True)
class Config:
    api_key: str = field(default="", repr=False)
    demo_password: str = field(default="", repr=False)
    workspace: str = ""
    region: str = "cn-beijing"
    model: str = "qwen-audio-3.0-realtime-flash"
    voice: str = ""
    live_enabled: bool = False
    origin: str = "http://127.0.0.1:8080"
    max_sessions: int = 2
    max_seconds: float = 600
    handshake_seconds: float = 15
    idle_seconds: float = 60
    stackchan_enabled: bool = False
    stackchan_device_id: str = ""
    stackchan_device_token: str = field(default="", repr=False)
    stackchan_read_only: bool = False
    stackchan_test_max_deg: float | None = None
    stackchan_audit_enabled: bool = False
    device_ack_seconds: float = 1.5
    device_keepalive_seconds: float = 10

    @classmethod
    def from_env(cls, env_file=DEFAULT_ENV_FILE):
        values = read_env_file(env_file)
        # A supplied process variable wins even when its value is empty.
        def get(name, default=""):
            return os.getenv(name, values.get(name, default))
        return cls(api_key=get("DASHSCOPE_API_KEY"),
                   demo_password=get("S2S_DEMO_PASSWORD"),
                   workspace=get("QWEN_WORKSPACE_ID"),
                   region=get("QWEN_REGION", "cn-beijing"),
                   model=get("QWEN_MODEL", "qwen-audio-3.0-realtime-flash"),
                   voice=get("QWEN_VOICE"),
                   live_enabled=get("S2S_ENABLE_LIVE") == "1",
                   origin=get("S2S_ORIGIN", "http://127.0.0.1:8080"),
                   stackchan_enabled=get("S2S_STACKCHAN_ENABLED") == "1",
                   stackchan_device_id=get("S2S_STACKCHAN_DEVICE_ID"),
                   stackchan_device_token=get("S2S_STACKCHAN_DEVICE_TOKEN"),
                   stackchan_test_max_deg=float(get("S2S_STACKCHAN_TEST_MAX_DEG"))
                       if get("S2S_STACKCHAN_TEST_MAX_DEG") else None,
                   stackchan_audit_enabled=get("S2S_STACKCHAN_AUDIT") == "1")

    @property
    def stackchan_configured(self):
        return bool(self.stackchan_enabled
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.stackchan_device_id)
                    and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.stackchan_device_token))

    def problem(self):
        if self.stackchan_test_max_deg is not None and (
                not math.isfinite(self.stackchan_test_max_deg) or not 0 < self.stackchan_test_max_deg <= 60):
            return "设备测试角度限制无效。"
        if not self.live_enabled:
            return "真实模型连接未启用；本地页面可用。"
        if not self.demo_password:
            return "真实模型连接需要配置服务端演示登录口令。"
        if not self.api_key:
            return "服务端缺少北京或所选地域的 DashScope API Key。"
        if self.workspace and not re.fullmatch(r"[A-Za-z0-9-]{1,128}", self.workspace):
            return "业务空间 ID 格式无效。"
        if not self.is_audio and not self.workspace:
            return "Omni 专属地址需要业务空间 ID；Audio 默认公共地址无需配置。"
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
        public_hosts = {"cn-beijing": "dashscope.aliyuncs.com",
                        "ap-southeast-1": "dashscope-intl.aliyuncs.com"}
        if self.region not in public_hosts:
            raise ValueError("Unsupported region")
        if self.workspace:
            if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", self.workspace):
                raise ValueError("Invalid workspace ID")
            host = f"{self.workspace}.{self.region}.maas.aliyuncs.com"
        elif self.is_audio:
            host = public_hosts[self.region]
        else:
            raise ValueError("Omni requires a workspace endpoint")
        return f"wss://{host}/api-ws/v1/realtime?" + urlencode({"model": self.model})

    def session_update(self, stackchan_tools=None):
        instructions = (
            "你是用户的个人助理 Agent。请用简洁自然的中文交流，帮助用户解答问题、思考和规划日常事务。"
            "语音回复要简短自然：动作成功或失败只口头说一句，优先不超过20个中文字符。"
            "不要朗读错误码、参数、计算过程或排查步骤；技术详情由界面文字诊断展示。"
            "普通对话保持简短，需要展开时等用户要求，避免长篇连续播报。"
            "StackChan 是可接入的设备工具，用于动作、表情、灯光和屏幕显示；仅在设备已连接、"
            "会话已授权且相应工具实际可用时调用。设备确认接收不代表执行完成，执行状态必须依据真实回执。"
            "持久记忆尚未接入，不要声称跨会话保存了记忆。"
        )
        # Normal speech exposes free targets rather than asking Flash to author
        # clocks. The full tool catalog and legacy perform executor remain intact.
        stackchan_tools = [voice_target_tool(tool) for tool in (stackchan_tools or [])
                           if tool.get('function', {}).get('name') != 'stackchan_perform']
        if stackchan_tools and self.is_audio:
            instructions += (
                "【设备动作】使用stackchan_perform_targets自由选择目标关键帧steps：姿态、表情、灯光与先后顺序。"
                "不填写at_ms、move_ms或duration_ms，不计算速度、时长或插帧；这些由网关和设备负责。"
                "steps直接使用JSON数组，不要再编码成字符串。"
                "每步直接填写yaw_deg、pitch_deg、expression或text、rgb和brightness、hold_ms；不填写head/screen/light/page包装字段。"
                "可以将独立hold_ms步骤放在目标后，代码会合并停顿；首步不能只有停顿。"
                "每个step内的输出同时开始，hold_ms表示到位后的停顿；pace可选gentle/normal/brisk。"
                "设备已绑定在线时，StackChan是个人助理的非语言表达载体，语言与目标动作共同构成自然短回应。"
                "明确否定、赞同等适合肢体表达的短回应，默认主动规划一次轻量姿态表达："
                "先按语义选择并调用目标工具，再简短回答原问题，不必等待用户另下动作口令。"
                "例如否定可轻摇头、赞同可轻点头、疑惑可轻歪头或配思考表情；动作由你自由选择，轻量低频，非固定手势菜单。"
                "轻摇头要生成左右方向交替的多个yaw目标，形成往返；单向转头不是摇头。"
                "轻点头也要有pitch俯仰往返，不能只有单向俯仰；幅度、中心和节奏由你按上下文自由选择，尊重明确保持的朝向。"
                "长信息回答、引用文本、上下文不适合时不强行动，不每句必动；用户要求保持不动时优先不动。"
                "普通问答配动作时，语音仍简短直接回答原问题，不要换成摇头完成等动作宣告或技术播报。"
                "轻量表达优先围绕当前或上下文指定朝向往返；上下文要求保持某朝向时，表达后返回该朝向，不能擅自回正。"
                "先查询fresh状态与能力，只选择已公布范围内、能表达用户意图的不同目标，不能把当前姿态重复到所有step。"
                "定向看/转向默认保持最终目标，明确设置return_to_neutral=false，不额外追加0/0目标。"
                "hold_ms结束不意味着恢复；只有明确回正/正视、往返动作意图或上下文表达确实需要回正时，"
                "才显式选择return_to_neutral=true，由代码明确回到yaw=0,pitch=0。"
                "允许自由选择上下文恰当的头部姿态配合表情与灯光，但不能每次默认追加回正；"
                "微笑用happy，大笑用laughing，按用户语义区分。用户明确展示表情时留足观察时间，"
                "单独展示通常停留1.5至2秒或保持到下个合适目标；未请求且语境不需要恢复时，不要每次主动追加neutral。"
                "用户明确向左或向右看时，应以该方向作为最终目标。不要把测得的偏移姿态当作中立。"
                "没有固定手势枚举，你自行选择空间目标，不按关键词触发脚本动作。"
                "accepted只代表接纳；按effective_duration_ms和get_command真实终态确认完成，不能提前说成功。"
                "simulated=true时明确仅模拟执行，simulated未知也不能声称真机已执行。"
                "无法放进设备边界的计划会整体拒绝，不裁剪拆分或自动反复尝试。"
                "参数错误仅在retryable=true时按details明确修正一次；retryable=false时结束本轮动作、等待新语音，不再调用动作工具。"
                "OUT_OF_RANGE不可猜成锁定或校准故障。"
                "只有NOT_CALIBRATED表示未校准，BUSY/DEVICE_BUSY表示占用。"
                "MOTION_TIMEOUT、SCHEDULER_OVERRUN、硬件异常或结果未知时停止后续动作，不自动重试。"
                "故障后本会话头部命令会被拒绝，stop不会清除；等用户确认设备并重新接通。"
                "技术错误仅界面文字诊断，不朗读参数或排查步骤。其他外部工具未启用。")
            instructions += ("电源读数是带sample_age_ms的缓存；单次电量百分比不是精确容量，"
                             "不能根据未刷新的电源数据推断USB已拔除，需结合用户确认及持续连接验证。")
            if self.stackchan_read_only:
                instructions += "当前联调只允许查询状态或命令，不执行perform动作。"
            if self.stackchan_test_max_deg is not None:
                instructions += f"当前测试头部两轴绝对目标均不得超过{self.stackchan_test_max_deg:g}度，不提高速度或放宽设备容差。"
        else:
            instructions += "当前会话未启用 StackChan 或其他外部工具，不要声称已操控设备或完成外部操作。"
        if self.is_audio:
            voice = self.voice or ("longanqian_v3.1" if "3.1" in self.model else "longanqian")
            return {"type": "session.update", "session": {
                "modalities": ["text", "audio"], "voice": voice,
                "input_audio_format": "pcm", "output_audio_format": "pcm",
                "turn_detection": {"type": "smart_turn"}, "instructions": instructions,
                **({"tools": stackchan_tools} if stackchan_tools else {}),
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
