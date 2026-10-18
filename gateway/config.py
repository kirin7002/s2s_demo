"""Server-only configuration. Never serialize this object to clients."""
from dataclasses import dataclass, field
import os
import math
import re
from urllib.parse import urlencode
from .envfile import DEFAULT_ENV_FILE, read_env_file


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
            "普通对话也先简短回答，需要展开时等用户要求，避免长篇连续播报。"
            "StackChan 是可接入的设备工具，用于动作、表情、灯光和屏幕显示；仅在设备已连接、"
            "会话已授权且相应工具实际可用时调用。设备确认接收不代表执行完成，执行状态必须依据真实回执。"
            "持久记忆尚未接入，不要声称跨会话保存了记忆。"
        )
        if stackchan_tools and self.is_audio:
            instructions += (
                "【头部默认节奏】用户未指定节奏的小幅点头/摇头，每段move_ms默认1000–1500ms，包含到位稳定时间。"
                "100ms只是schema最小允许值，不是默认节奏；也不要直接把速度公式的最小值作为实际move_ms。"
                "若距离/速度计算结果加250ms超过默认时长，应相应延长，并同步重算at_ms和duration_ms。"
                "点头须选择不同俯仰目标最后回软件pitch=0；摇头须选择左右不同yaw目标最后回软件yaw=0。"
                "目标和幅度由你根据用户意图与新鲜设备能力选择，不能只重复当前姿态。")
            instructions += ("当前会话已注册 StackChan 工具；请先查询设备状态和能力，仅使用设备公布的能力。"
                             "accepted仅代表接纳，请查询命令结果再声称完成；超时未知时不要重复物理动作。"
                             "工具结果simulated=true时明确说明仅为模拟执行，不能声称真实机器人已动作；"
                             "simulated未知时也不能声称真实设备已执行。"
                             "同一设备的动作可先停止再执行，不根据固定话术强制动作。其他外部工具未启用。")
            instructions += (
                "头部动作先读取fresh实际姿态和capabilities；首帧从该实际姿态计算，以后从前一个头部目标计算。"
                "当前姿态仅是计算首段距离的起点，不是把它复制到每帧的动作目标。"
                "理解用户意图后自行选择可见且有区别的目标：点头要有俯仰变化并回中，摇头要有左右不同的yaw目标并回中。"
                "连续相同姿态只是保持，不是点头或摇头；即使命令completed也不能声称完成了用户要求的动作。"
                "每段distance=max(abs(目标yaw-起点yaw),abs(目标pitch-起点pitch))；"
                "move_ms至少ceil(distance/max_speed_deg_s*1000)，建议额外加250ms稳定裕量。"
                "例如速度30度/秒，yaw从-20到+20至少1334ms，建议1584ms，不能每段500ms。"
                "同步重算后续at_ms和总duration_ms，保持同通道不重叠、全部帧结束于总时长内。"
                "遵守最多16帧/8000ms和move_ms最大4000ms；若装不下，减少重复次数或向用户简短提出减幅方案，"
                "不要擅自改变用户明确的动作要求或放宽速度/限位，不用200ms连续极限摆动。"
                "OUT_OF_RANGE按details.reason区分target、speed、frame_count、duration或move_duration，"
                "根据frame_index、minimum_move_ms和limits解释并重新规划，不可猜测锁定、未校准或建议重启。"
                "只有NOT_CALIBRATED才说明未校准；BUSY/DEVICE_BUSY说明被占用，MOTION_TIMEOUT说明未按时到位。"
                "相同错误不得盲目多次重试；参数明确可修正时最多重新规划一次，仍失败就简短告知用户。"
                "MOTION_TIMEOUT、SCHEDULER_OVERRUN、硬件异常或执行结果未知时停止后续动作，不自动重复，技术细节不进入语音。"
                "头部硬件故障后网关会拒绝本会话后续头部命令，stop不会清除此故障；告知用户停止并检查设备，"
                "由用户确认设备后重新接通，不能自行改时长重试。")
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
