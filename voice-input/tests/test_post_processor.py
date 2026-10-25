#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PostProcessor 两级后处理单元测试。

无头可跑：纯内存字符串变换 + 对不可达本地端口的降级测试，
不依赖音频硬件、X11、模型文件与真实 LLM 服务。
"""

import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voice_input.post_processor as ppm
from voice_input.post_processor import PostProcessor


def make_level1(**overrides):
    cfg = {
        "clean_fillers": True,
        "auto_space_cjk_ascii": True,
        "custom_replacements": {"新的一行": "\n", "回车换行": "\n"},
    }
    cfg.update(overrides)
    return PostProcessor(cfg)


class TestLevel1Cleaning(unittest.TestCase):
    def setUp(self):
        self.pp = make_level1()

    def test_leading_filler_phrases_stripped(self):
        self.assertEqual(self.pp.clean_level1("嗯，那个，你好"), "你好")

    def test_leading_real_word_kept(self):
        # “那个”后接实义字（非标点）时是限定词，必须保留。
        self.assertEqual(self.pp.clean_level1("那个东西很好"), "那个东西很好")

    def test_mid_hanzi_filler_removed(self):
        self.assertEqual(
            self.pp.clean_level1("连续说话模式呃分开一下"),
            "连续说话模式分开一下",
        )

    def test_medical_erer_kept(self):
        # 句中“呃逆”是实义词：_MID_HANZI_FILLER_RE 的 (?!逆) 守卫不得误删。
        # 注：句首的“呃逆”会被句首语气词规则吞掉，这里测句中守卫语义。
        out = self.pp.clean_level1("听说呃逆要去看医生")
        self.assertIn("呃逆", out)

    def test_cjk_ascii_spacing(self):
        self.assertEqual(
            self.pp.clean_level1("使用GPU跑了3次实验"),
            "使用 GPU 跑了 3 次实验",
        )

    def test_custom_replacement_xinhang(self):
        self.assertEqual(self.pp.clean_level1("标题新的一行正文"), "标题\n正文")

    def test_custom_replacement_huiche(self):
        self.assertEqual(
            self.pp.clean_level1("第一段回车换行第二段"), "第一段\n第二段"
        )


class TestLevel2Fallback(unittest.TestCase):
    def _unreachable_pp(self):
        return PostProcessor(
            {
                "clean_fillers": True,
                "auto_space_cjk_ascii": True,
                "custom_replacements": {"新的一行": "\n", "回车换行": "\n"},
                "enable_llm": True,
                "api_base": "http://127.0.0.1:9/v1",  # 不可达端口
                "api_key": "EMPTY",
                "model": "test-model",
                "timeout": 0.2,
            }
        )

    def _direct_env(self):
        # 绕开可能存在的代理，确保连 refused 直达、快速失败。
        env = {
            "http_proxy": "",
            "https_proxy": "",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "all_proxy": "",
            "ALL_PROXY": "",
            "no_proxy": "127.0.0.1,localhost",
            "NO_PROXY": "127.0.0.1,localhost",
        }
        return mock.patch.dict("os.environ", env)

    def test_llm_unreachable_falls_back_fast(self):
        pp = self._unreachable_pp()
        self.assertTrue(pp.enable_llm)  # 配置齐全，保持开启才会走降级路径
        expected = pp.clean_level1("嗯，那个，你好")
        with self._direct_env():
            t0 = time.monotonic()
            out = pp.process("嗯，那个，你好")
            dt = time.monotonic() - t0
        self.assertEqual(out, expected)
        self.assertLess(dt, 0.5, f"降级耗时 {dt:.3f}s 超过 0.5s 上限")

    def test_think_tag_stripped_by_regex(self):
        raw = "<think>这是\n多行思考过程</think>最终答案"
        self.assertEqual(ppm._THINK_TAG_RE.sub("", raw).strip(), "最终答案")

    def test_think_tag_stripped_in_call(self):
        pp = self._unreachable_pp()
        fake_resp = mock.Mock()
        fake_resp.raise_for_status.return_value = None
        fake_resp.json.return_value = {
            "choices": [{"message": {"content": "<think>先想想</think>纠错后的文本"}}]
        }
        with mock.patch.object(ppm, "requests") as mreq:
            mreq.post.return_value = fake_resp
            out = pp._call_llm("原始文本")
        self.assertEqual(out, "纠错后的文本")
        # disable_thinking 默认 True：payload 顶层必须注入关闭 thinking 的开关
        _, kwargs = mreq.post.call_args
        sent = kwargs.get("json", {})
        self.assertEqual(
            sent.get("chat_template_kwargs"), {"enable_thinking": False}
        )

    def test_sanitize_rejects_garbage(self):
        pp = self._unreachable_pp()
        self.assertIsNone(pp._sanitize_llm_output("原文", None))
        self.assertIsNone(pp._sanitize_llm_output("原文", "   "))
        # 输出异常膨胀（> 2 倍 + 30）视为不可信
        self.assertIsNone(pp._sanitize_llm_output("短", "很" * 100))


if __name__ == "__main__":
    unittest.main()
