"""语音识别结果的两级后处理。

* **Level 1 — 确定性规则清洗（默认开启，零延迟）**
  去口头禅、中英文间距美化、口述符号 / 自定义术语替换。
  纯本地正则 + 字符串操作，不会引入任何网络等待。

* **Level 2 — 可选 LLM 极速纠错（默认关闭）**
  开启后调用兼容 OpenAI 协议的 Chat Completions 接口做同音字 / 语法纠错。
  为保证"绝不阻塞用户输入"，这里设置 **1.5 秒硬超时** 与多重护栏，
  任何超时 / 报错 / 异常输出都会静默降级到 Level 1 的结果。
  针对本地 ``qwen3.8-27b`` 这类默认开启 reasoning 的模型，可通过
  ``disable_thinking: True`` 在请求 payload 顶层注入
  ``chat_template_kwargs: {"enable_thinking": false}``，让模型直接输出结果、
  不再生成 ``<think>`` 思考过程；响应中若仍残留 ``<think>...</think>`` 也会被剥离。

用法::

    pp = PostProcessor({"custom_replacements": {"新的一行": "\n"}})
    final_text = pp.process(raw_text)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:  # requests 仅在开启 Level 2 时才真正需要
    import requests
except ImportError:  # pragma: no cover - 取决于运行环境
    requests = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# 正则常量
# --------------------------------------------------------------------------- #
# CJK 常用汉字区间（含扩展 A、兼容表意文字），用于中英混排间距。
CJK_RANGE = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"

# 单个语气词（可重复，如 "嗯嗯""啊啊"），排除 "呃逆"、"额外/额度/额头" 等实词开头。
_LEADING_SINGLE_FILLER_RE = re.compile(
    rf"^(?:(?:嗯+|呃+(?!逆)|额+(?!外|度|头|角|定|满)|啊+|唉+|哦+|唔+|噢+|诶+))[，,、。.\s]*"
)
# 成句的口头禅，必须后接标点才删，避免误删 "那个东西" 里的实义"那个"。
_LEADING_PHRASE_FILLER_RE = re.compile(
    r"^(?:(?:那个|这个|就是说|然后就是|就是))[，,、。.\s]+"
)
# 句中孤立的语气词：两侧都是标点时才删，避免误删 "好啊""天啊"。
_MID_FILLER_RE = re.compile(
    r"(?<=[，,、。！？!?])\s*(?:嗯+|呃+|额+|啊+|唉+|哦+|唔+|噢+|诶+)\s*(?=[，,、。！？!?])"
)
# 夹在汉字之间的口语停顿词 "呃+"（如 "连续说话模式呃分开一下" -> "连续说话模式分开一下"）。
# 用负向先行断言排除 "呃逆" 等实义词，避免误删。
_MID_HANZI_FILLER_RE = re.compile(
    rf"(?<=[{CJK_RANGE}])呃+(?!逆)(?=[{CJK_RANGE}])"
)

# 标点收敛：连续的逗号/顿号，以及句末标点前的多余逗号。
_DUP_SEPARATOR_RE = re.compile(r"([，,、])\s*(?=[，,、])")
_SEPARATOR_BEFORE_END_RE = re.compile(r"[，,、]\s*(?=[。！？!?])")

_CJK_BEFORE_ASCII_RE = re.compile(rf"([{CJK_RANGE}])([A-Za-z0-9])")
_ASCII_BEFORE_CJK_RE = re.compile(rf"([A-Za-z0-9])([{CJK_RANGE}])")

_MULTI_SPACE_RE = re.compile(r"[ \t]{2,}")

# 剥离模型可能残留的 <think>...</think> 思考过程（DOTALL 允许跨行）。
_THINK_TAG_RE = re.compile(r"<think>.*?</think>", flags=re.DOTALL)


DEFAULT_SYSTEM_PROMPT = (
    "你是实时语音输入法的同音字纠错与顺句助手。"
    "请将用户口述的语音识别草稿修正为通顺、自然的书面或日常用语："
    "1. 修正同音/近音错别字（如'这的是'改为'这是'）与不通顺的语序；"
    "2. 删去多余口头禅（嗯、呃、啊、那个）与口误重复；"
    "3. 严格保留原意、句式（疑问句仍为疑问句，绝不回答问题！）与专业术语；"
    "4. 只输出修正后的最终文本，严禁附带任何解释、引号或前缀。"
)


class PostProcessor:
    """ASR 文本后处理器（Level 1 规则 + Level 2 可选 LLM 润色）。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        """从配置字典初始化。

        支持的配置项（均有安全默认值，缺失即用默认）::

            {
                # Level 1
                "auto_space_cjk_ascii": True,     # 中英文间距美化
                "clean_fillers": True,            # 去口头禅
                "custom_replacements": {"新的一行": "\n", "回车换行": "\n"},
                "preserve_newlines": True,        # 替换产生的换行不被压掉

                # Level 2（默认关闭）
                "enable_llm": False,
                "api_base": "http://127.0.0.1:8000/v1",
                "api_key": "EMPTY",
                "model": "qwen3.8-27b",
                "timeout": 1.5,                   # 硬超时（秒），不建议调大
                "disable_thinking": True,         # 关闭 reasoning（注入 enable_thinking=false）
                "llm_backend": "openai",          # "openai" 走 Chat Completions；"opencode_cli" 走本机 OpenCode CLI
                "opencode_bin": "opencode",       # OpenCode CLI 可执行文件名（走 PATH 解析）
                "opencode_model": "opencode/deepseek-v4.1-flash-free",
                "opencode_agent": "",             # 为空则不传 --agent
                "opencode_timeout": 30.0,         # CLI 单次调用超时（秒），免费档单次约 2~3 秒
                "llm_min_chars": 2,               # 太短的文本不值得润色
                "llm_max_chars": 1000,            # 太长的文本不做 LLM，防止超时
            }
        """
        cfg = dict(config or {})
        self.config = cfg

        # ---- Level 1 ----
        self.auto_space_cjk_ascii = bool(cfg.get("auto_space_cjk_ascii", True))
        self.clean_fillers = bool(cfg.get("clean_fillers", True))
        self.preserve_newlines = bool(cfg.get("preserve_newlines", True))
        # 长词优先替换，避免 "回车换行" 被 "换行" 抢先命中导致残留。
        self.custom_replacements: List[tuple[str, str]] = sorted(
            (
                (str(k), str(v))
                for k, v in dict(cfg.get("custom_replacements") or {}).items()
                if str(k)
            ),
            key=lambda kv: len(kv[0]),
            reverse=True,
        )

        # ---- Level 2 ----
        self.enable_llm = bool(cfg.get("enable_llm", False))
        self.api_base = str(cfg.get("api_base") or "").rstrip("/")
        self.api_key = str(cfg.get("api_key") or "")
        self.model = str(cfg.get("model") or "")
        self.timeout = float(cfg.get("timeout", 1.5))
        self.disable_thinking = bool(cfg.get("disable_thinking", True))
        self.llm_backend = str(cfg.get("llm_backend") or "openai")
        self.opencode_bin = str(cfg.get("opencode_bin") or "opencode")
        self.opencode_model = str(cfg.get("opencode_model") or "opencode/deepseek-v4.1-flash-free")
        self.opencode_agent = str(cfg.get("opencode_agent") or "")
        self.opencode_timeout = float(cfg.get("opencode_timeout", 30.0))
        self.llm_min_chars = int(cfg.get("llm_min_chars", 2))
        self.llm_max_chars = int(cfg.get("llm_max_chars", 1000))
        self.llm_system_prompt = str(cfg.get("llm_system_prompt") or DEFAULT_SYSTEM_PROMPT)

        if self.enable_llm and self.llm_backend != "opencode_cli" and not self._llm_configured():
            logger.warning("enable_llm=True 但缺少 api_base/api_key/model，已自动停用 LLM 润色")
            self.enable_llm = False

    # ------------------------------------------------------------------ #
    # 对外主入口
    # ------------------------------------------------------------------ #
    def process(self, text: str) -> str:
        """执行完整后处理流程，返回最终文本。"""
        cleaned = self.clean_level1(text or "")
        if not self.enable_llm or not cleaned:
            return cleaned
        if self.llm_backend == "opencode_cli":
            return self.polish_opencode_cli(cleaned)
        return self.polish_level2(cleaned)

    # ------------------------------------------------------------------ #
    # Level 1：确定性规则清洗（零延迟）
    # ------------------------------------------------------------------ #
    def clean_level1(self, text: str) -> str:
        """去口头禅 + 自定义替换 + 中英文间距美化。"""
        if not text:
            return ""

        result = text

        # 1) 口述符号 / 命令词 / 术语纠错（用户自定义，最长词优先）。
        result = self._apply_replacements(result)

        # 2) 去句首与句中孤立的语气词。
        if self.clean_fillers:
            result = self._strip_fillers(result)

        # 3) 中英文间距美化。
        if self.auto_space_cjk_ascii:
            result = self._space_cjk_ascii(result)

        # 4) 收尾：压缩多余空格（保留换行），去掉首尾空白。
        if self.preserve_newlines:
            result = "\n".join(_MULTI_SPACE_RE.sub(" ", line).strip() for line in result.split("\n"))
            result = result.strip("\n").strip()
        else:
            result = _MULTI_SPACE_RE.sub(" ", result).strip()

        return result

    def _apply_replacements(self, text: str) -> str:
        """按配置词典做逐条字符串替换（长词优先）。"""
        for src, dst in self.custom_replacements:
            if src in text:
                text = text.replace(src, dst)
        return text

    @staticmethod
    def _strip_fillers(text: str) -> str:
        """删除句首 / 句中孤立的无意义口头禅语气词。

        句首：``嗯、呃、啊、额、唉、哦、唔、噢、诶`` 可连写，后面可跟标点；
              ``那个 / 这个 / 就是说 / 然后就是 / 就是`` 必须后接标点才删。
        句中：只有当语气词两侧都是标点时（如 "我喜欢，啊，这个"）才删；
              另外删除夹在汉字之间的口语停顿词 ``呃+``（如 "模式呃分开" -> "模式分开"），
              但保留 "呃逆" 等实义词。
        """
        prev = None
        # 句首可能连续出现多个口头禅，循环直到不再变化。
        while prev != text:
            prev = text
            text = _LEADING_SINGLE_FILLER_RE.sub("", text)
            text = _LEADING_PHRASE_FILLER_RE.sub("", text)

        # 句中（含句首删完后新暴露出来的部分）。
        text = _MID_FILLER_RE.sub("", text)

        # 夹在汉字之间的口语停顿词 "呃+"（排除 "呃逆"）。
        text = _MID_HANZI_FILLER_RE.sub("", text)

        # 删掉逗号后可能留下连续标点（"我喜欢，啊，这个" -> "我喜欢，，这个"），
        # 这里做标点收敛："，，" -> "，"、"。"前的多余逗号 -> 去掉。
        text = _DUP_SEPARATOR_RE.sub("", text)
        text = _SEPARATOR_BEFORE_END_RE.sub("", text)

        # 句首删完可能紧接标点，清理掉开头的孤立标点。
        text = re.sub(r"^[，,、]\s*", "", text)
        return text

    @staticmethod
    def _space_cjk_ascii(text: str) -> str:
        """在汉字与 ASCII 字母 / 数字之间补一个空格（只处理汉字/ASCII 交界）。

        只在交界处插入空格，因此对已有空格、标点、纯英文/纯中文文本均无副作用。
        """
        text = _CJK_BEFORE_ASCII_RE.sub(r"\1 \2", text)
        text = _ASCII_BEFORE_CJK_RE.sub(r"\1 \2", text)
        return text

    # ------------------------------------------------------------------ #
    # Level 2：可选 LLM 极速纠错（默认关闭，失败绝不阻塞）
    # ------------------------------------------------------------------ #
    def _llm_configured(self) -> bool:
        """是否具备调用 LLM 的必要配置。"""
        return bool(self.api_base and self.api_key and self.model and requests is not None)

    def _call_llm(self, text: str) -> Optional[str]:
        """调用 Chat Completions 接口，返回模型输出的纯文本内容。

        - 当 ``disable_thinking=True`` 时，在 payload 顶层注入
          ``chat_template_kwargs: {"enable_thinking": false}``，让
          ``qwen3.8-27b`` 等默认开启 reasoning 的模型跳过思考、直接给答案。
        - 响应内容中若仍残留 ``<think>...</think>``，用正则剥离干净。

        任何网络 / 超时 / 格式异常都返回 ``None``（由调用方降级）。
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.llm_system_prompt},
                {"role": "user", "content": text},
            ],
            "temperature": 0.0,
            "max_tokens": min(512, max(64, len(text) * 2)),
            "stream": False,
        }
        # 关闭 reasoning：必须在顶层传入 chat_template_kwargs（vLLM / 千问模板约定）。
        if self.disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(
                f"{self.api_base}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout,  # 硬超时：超时即降级，绝不阻塞用户输入
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:  # 超时、连接失败、鉴权失败、格式异常……
            logger.debug("LLM 纠错失败，降级为 Level 1 结果：%s", exc)
            return None

        if not isinstance(content, str):
            return None
        # 剥离可能残留的思考过程，只保留最终答案。
        return _THINK_TAG_RE.sub("", content).strip()

    def _guard_llm_input(self, text: str) -> Optional[str]:
        """LLM 纠错共用护栏 1/2：长度上下限 + 纯符号过滤，不通过返回 None。"""
        stripped = text.strip()
        if len(stripped) < self.llm_min_chars or len(stripped) > self.llm_max_chars:
            logger.debug("文本长度 %d 不在 [%d, %d]，跳过 LLM 纠错",
                         len(stripped), self.llm_min_chars, self.llm_max_chars)
            return None
        if not re.search(r"[A-Za-z0-9\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", stripped):
            return None
        return stripped

    @staticmethod
    def _sanitize_llm_output(stripped: str, polished: Optional[str]) -> Optional[str]:
        """LLM 纠错共用护栏 3：剥离引号 + 输出合理性检查，不可信返回 None。"""
        if not polished:
            return None
        polished = _THINK_TAG_RE.sub("", polished).strip().strip('"').strip("“”").strip()
        if not polished:
            return None
        if len(polished) > len(stripped) * 2 + 30:
            logger.debug("LLM 输出异常膨胀（%d -> %d），放弃纠错",
                         len(stripped), len(polished))
            return None
        return polished

    def polish_level2(self, text: str) -> str:
        """调用 LLM 做轻度纠错；任何异常 / 超时都降级返回原文本。

        Args:
            text: 已经过 Level 1 清洗的文本。

        Returns:
            纠错后的文本；未启用、护栏拦截或调用失败时原样返回 ``text``。
        """
        if not self.enable_llm or not text:
            return text
        if not self._llm_configured():
            return text

        stripped = self._guard_llm_input(text)
        if stripped is None:
            return text

        polished = self._sanitize_llm_output(stripped, self._call_llm(stripped))
        return polished if polished is not None else text

    # ------------------------------------------------------------------ #
    # OpenCode CLI 后端（免费档，走本机 OpenCode 登录态，无需 Key）
    # ------------------------------------------------------------------ #
    def polish_opencode_cli(self, text: str) -> str:
        """经本机 ``opencode run`` CLI 做纠错；任何失败都降级返回原文本。

        Args:
            text: 已经过 Level 1 清洗的文本。

        Returns:
            纠错后的文本；未启用、护栏拦截或调用失败时原样返回 ``text``。
        """
        if not self.enable_llm or not text:
            return text

        stripped = self._guard_llm_input(text)
        if stripped is None:
            return text

        message = (
            f"{self.llm_system_prompt}\n\n待纠错文本：\n{stripped}"
            "\n\n只输出修正后的最终文本，不要调用任何工具。"
        )
        cmd = [self.opencode_bin, "run", "--model", self.opencode_model]
        if self.opencode_agent:
            cmd += ["--agent", self.opencode_agent]
        cmd += ["--format", "json", message]

        try:
            proc = subprocess.run(  # noqa: S603 - argv 传参，无 shell 注入面
                cmd,
                capture_output=True,
                text=True,
                timeout=self.opencode_timeout,
                env=dict(os.environ),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.debug("opencode CLI 纠错失败，降级为 Level 1 结果：%s", exc)
            return text
        if proc.returncode != 0:
            logger.debug("opencode CLI 非零退出（%s），降级为 Level 1 结果：%s",
                         proc.returncode, (proc.stderr or "").strip()[-500:])
            return text

        polished = self._sanitize_llm_output(stripped, self._parse_opencode_stdout(proc.stdout or ""))
        return polished if polished is not None else text

    @staticmethod
    def _parse_opencode_stdout(stdout: str) -> str:
        """解析 ``opencode run --format json`` 的 JSON-lines 事件流。

        纠错文本在 ``{"type": "text", "part": {"text": "..."}}`` 行的
        ``part.text`` 里；step_start / step_finish 等无关行直接跳过。
        解析失败时退化为：拼接所有能解析出的 part.text，仍为空则取原文 strip。
        """
        texts: List[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(evt, dict) or evt.get("type") != "text":
                continue
            part = evt.get("part") or {}
            chunk = part.get("text") if isinstance(part, dict) else None
            if isinstance(chunk, str) and chunk:
                texts.append(chunk)
        if texts:
            return "".join(texts)
        return stdout.strip()
