"""按 AstrBot「分段回复」规则拆分 LLM 输出，并逐句决定语音/文字。

- 分段规则完全读取 AstrBot 的 platform_settings.segmented_reply：
  regex / split_words、words_count_threshold、content_cleanup_rule、only_llm_result
- 每段独立掷骰：概率来自 virtual_life 写在 event extra 里的
  virtual_life_voice_probability（忙 0.7 / 空闲 0.2），没有则用插件配置的兜底值
- 命中语音的段落调用 astrbot_plugin_voice_file 合成语音，替换为 Record；
  未命中的段落保留为 Plain，由 RespondStage 按原间隔逐条发送

注意：启用本插件后应关闭 AstrBot 自带的「分段回复」，否则会分段两次
（虽然结果基本幂等，但没必要）。
"""

from __future__ import annotations

import asyncio
import random
import re

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star

DEFAULT_REGEX = r".*?[。？！~…]+|.+$"
SPLIT_SKIP_PLATFORMS = {"qq_official_webhook", "weixin_official_account", "dingtalk"}
VOICE_PLUGIN_ID = "astrbot_plugin_voice_file"


class VoiceSplitPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    # ------------------------------------------------------------------ 分段

    def _segmented_settings(self) -> dict:
        config = self.context.get_config()
        platform_settings = getattr(config, "platform_settings", None)
        if platform_settings is None and isinstance(config, dict):
            platform_settings = config.get("platform_settings") or {}
        settings = getattr(platform_settings, "segmented_reply", None)
        if settings is None and isinstance(platform_settings, dict):
            settings = platform_settings.get("segmented_reply") or {}
        return dict(settings or {})

    @staticmethod
    def _split_by_words(text: str, split_words: list[str]) -> list[str]:
        if not split_words:
            return [text]
        escaped = sorted(
            [re.escape(word) for word in split_words],
            key=len,
            reverse=True,
        )
        pattern = re.compile(f"(.*?({'|'.join(escaped)})|.+$)", re.DOTALL)
        segments = pattern.findall(text)
        result: list[str] = []
        for segment in segments:
            if isinstance(segment, tuple):
                content = segment[0]
                if not isinstance(content, str):
                    continue
                for word in split_words:
                    if content.endswith(word):
                        content = content[: -len(word)]
                        break
                if content.strip():
                    result.append(content)
            elif segment and segment.strip():
                result.append(segment)
        return result if result else [text]

    def _split_text(self, text: str, settings: dict) -> list[str]:
        if not text:
            return []
        threshold = int(settings.get("words_count_threshold", 0) or 0)
        if len(text) > threshold:
            return [text]

        split_mode = str(settings.get("split_mode", "regex") or "regex")
        if split_mode == "words":
            parts = self._split_by_words(text, settings.get("split_words") or [])
        else:
            pattern = settings.get("regex") or DEFAULT_REGEX
            try:
                parts = re.findall(pattern, text, re.DOTALL | re.MULTILINE)
            except re.error:
                logger.error(
                    "[voice_split] 分段正则无效，使用默认正则：%s",
                    pattern,
                )
                parts = re.findall(DEFAULT_REGEX, text, re.DOTALL | re.MULTILINE)

        cleanup = settings.get("content_cleanup_rule")
        result: list[str] = []
        for part in parts:
            if isinstance(part, tuple):
                part = part[0] if part else ""
            segment = str(part)
            if cleanup:
                try:
                    segment = re.sub(cleanup, "", segment)
                except re.error:
                    logger.error("[voice_split] cleanup 正则无效，忽略：%s", cleanup)
                    cleanup = None
            segment = segment.strip()
            if segment:
                result.append(segment)
        return result or [text]

    # ------------------------------------------------------------------ 语音

    def _voice_plugin(self):
        star = self.context.get_registered_star(VOICE_PLUGIN_ID)
        plugin = getattr(star, "star_cls", None) if star else None
        if plugin is None or not hasattr(plugin, "synthesize_voice"):
            return None
        return plugin

    def _probability(self, event: AstrMessageEvent) -> float:
        raw = event.get_extra("virtual_life_voice_probability")
        if raw is None:
            raw = self.config.get("fallback_probability", 0.0)
        try:
            probability = float(raw)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, probability))

    # ------------------------------------------------------------------ 钩子

    @filter.on_decorating_result(priority=-1000)
    async def split_and_maybe_voice(self, event: AstrMessageEvent) -> None:
        if not self.config.get("enable", True):
            return
        result = event.get_result()
        if result is None or not result.chain:
            return

        settings = self._segmented_settings()
        if bool(settings.get("only_llm_result", True)) and not result.is_model_result():
            return
        if event.get_platform_name() in SPLIT_SKIP_PLATFORMS:
            return

        probability = self._probability(event)
        voice_plugin = self._voice_plugin() if probability > 0 else None
        if probability > 0 and voice_plugin is None:
            logger.warning("[voice_split] 找不到语音插件，全部按文字发送")
            probability = 0.0

        new_chain: list = []
        pending: list[tuple[int, str]] = []
        for component in result.chain:
            if not isinstance(component, Plain):
                new_chain.append(component)
                continue
            for segment in self._split_text(component.text or "", settings):
                if probability > 0 and random.random() < probability:
                    pending.append((len(new_chain), segment))
                    new_chain.append(None)
                else:
                    new_chain.append(Plain(segment))

        if pending and voice_plugin is not None:
            limit = int(self.config.get("max_concurrent_tts", 3) or 3)
            semaphore = asyncio.Semaphore(max(1, limit))

            async def _synthesize(text: str):
                async with semaphore:
                    return await voice_plugin.synthesize_voice(text)

            results = await asyncio.gather(
                *(_synthesize(text) for _, text in pending),
                return_exceptions=True,
            )
            voice_count = 0
            for (index, text), wav in zip(pending, results):
                if isinstance(wav, BaseException) or wav is None:
                    logger.warning("[voice_split] 语音合成失败，回退文字：%s", wav)
                    new_chain[index] = Plain(text)
                else:
                    new_chain[index] = Record(file=str(wav))
                    voice_count += 1
        else:
            voice_count = 0

        result.chain = new_chain
        logger.info(
            "[voice_split] segments=%s voice=%s p=%.2f",
            len(new_chain),
            voice_count,
            probability,
        )
