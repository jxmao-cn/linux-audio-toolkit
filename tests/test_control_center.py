#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""control_center 配置档位推断与切换写回单元测试。

无头可跑：临时目录拷贝 ``config.example.yaml`` 做隔离配置；
``ControlCenter(run_sh="/bin/true")`` 让切换后的守护进程重启步骤
恒为成功，不触碰真实语音服务、docker 与系统电源。
"""

import os
import shutil
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voice_input import control_center as cc

EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config.example.yaml",
)

# 切换写回后必须原样保留的注释（取自 config.example.yaml 后处理段）。
MUST_KEEP_COMMENT = "# 去除句首 / 句中孤立的无意义语气词"


class ControlCenterTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="voice-input-test-")
        self.cfg_path = os.path.join(self.tmp, "config.yaml")
        shutil.copy(EXAMPLE, self.cfg_path)
        self.cc = cc.ControlCenter(
            config_path=self.cfg_path,
            pidfile=os.path.join(self.tmp, "voice-input.pid"),
            logfile=os.path.join(self.tmp, "voice-input.log"),
            run_sh="/bin/true",  # restart 恒成功，不碰真实服务
        )
        # 指向不存在的 compose 文件：local_qwen 档走“未部署”分支，
        # 不调用 docker，测试完全 hermetic。
        cc.update_config_values(
            self.cfg_path,
            [(("control_center",), "qwen_compose_file",
              os.path.join(self.tmp, "no-such-compose.yml"))],
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def current_pp(self):
        return self.cc.status()["post_processor"]

    def assertCommentsKeptAndValid(self):
        with open(self.cfg_path, "r", encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(MUST_KEEP_COMMENT, text)
        parsed = yaml.safe_load(text)
        self.assertIsInstance(parsed, dict)
        self.assertIn("post_processor", parsed)
        return parsed


class TestInferMode(ControlCenterTestBase):
    def test_initial_example_is_off(self):
        self.assertEqual(self.cc.status()["llm_mode"], "off")

    def test_switch_off(self):
        r = self.cc.switch_mode("off")
        self.assertTrue(r["ok"])
        self.assertEqual(self.cc.status()["llm_mode"], "off")
        self.assertFalse(self.current_pp()["enable_llm"])
        self.assertCommentsKeptAndValid()

    def test_switch_local_qwen(self):
        r = self.cc.switch_mode("local_qwen")
        self.assertTrue(r["ok"], r)
        st = self.cc.status()
        self.assertEqual(st["llm_mode"], "local_qwen")
        pp = st["post_processor"]
        self.assertTrue(pp["enable_llm"])
        self.assertIn("127.0.0.1:8000", pp["api_base"])
        self.assertCommentsKeptAndValid()

    def test_switch_deepseek_needs_key_first(self):
        # 模板 Key 为空时拒绝切换（防止配错档）。
        r = self.cc.switch_mode("deepseek")
        self.assertFalse(r["ok"])
        self.assertEqual(self.cc.status()["llm_mode"], "off")

    def test_switch_deepseek_with_key(self):
        saved = self.cc.save_deepseek_key("sk-test-key-1234567890")
        self.assertTrue(saved["ok"])
        r = self.cc.switch_mode("deepseek")
        self.assertTrue(r["ok"], r)
        st = self.cc.status()
        self.assertEqual(st["llm_mode"], "deepseek")
        self.assertIn("api.deepseek.com", st["post_processor"]["api_base"])
        parsed = self.assertCommentsKeptAndValid()
        self.assertEqual(
            parsed["llm_modes"]["deepseek"]["api_key"], "sk-test-key-1234567890"
        )

    def test_switch_opencode(self):
        r = self.cc.switch_mode("opencode")
        self.assertTrue(r["ok"], r)
        st = self.cc.status()
        self.assertEqual(st["llm_mode"], "opencode")
        self.assertEqual(st["post_processor"]["llm_backend"], "opencode_cli")
        parsed = self.assertCommentsKeptAndValid()
        self.assertEqual(
            parsed["post_processor"]["opencode_model"],
            parsed["llm_modes"]["opencode"]["opencode_model"],
        )

    def test_unknown_mode_rejected(self):
        r = self.cc.switch_mode("not_a_mode")
        self.assertFalse(r["ok"])

    def test_roundtrip_back_to_off(self):
        self.assertTrue(self.cc.switch_mode("local_qwen")["ok"])
        self.assertEqual(self.cc.status()["llm_mode"], "local_qwen")
        self.assertTrue(self.cc.switch_mode("off")["ok"])
        st = self.cc.status()
        self.assertEqual(st["llm_mode"], "off")
        self.assertCommentsKeptAndValid()


class TestInferModePure(unittest.TestCase):
    """infer_mode 本体：纯字典推断，不碰文件系统。"""

    def test_off(self):
        self.assertEqual(cc.infer_mode({"enable_llm": False}), "off")
        self.assertEqual(cc.infer_mode({}), "off")

    def test_local_qwen(self):
        self.assertEqual(
            cc.infer_mode(
                {"enable_llm": True, "api_base": "http://127.0.0.1:8000/v1"}
            ),
            "local_qwen",
        )

    def test_deepseek(self):
        self.assertEqual(
            cc.infer_mode(
                {"enable_llm": True, "api_base": "https://api.deepseek.com/v1"}
            ),
            "deepseek",
        )

    def test_opencode(self):
        self.assertEqual(
            cc.infer_mode({"enable_llm": True, "llm_backend": "opencode_cli"}),
            "opencode",
        )


if __name__ == "__main__":
    unittest.main()
