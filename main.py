from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.message_components import Record, Plain, Image, At, Reply, AtAll
from pathlib import Path
import re
import aiohttp
import json
import random
import asyncio
import os
import uuid
import time
import hashlib
from datetime import datetime

# 注册插件的装饰器
@register("astrbot_plugin_VITS_pro", "Chris95743/第九位魔神", "语音合成插件", "1.7.0")
class VITSPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.context = context  # 保存context引用用于配置更新
        self.api_url = config.get('url', '')  # 提取 API URL
        self.api_key = config.get('apikey', '')  # 提取 API Key
        self.api_name = config.get('name', '')  # 提取 模型 名称
        self.api_voice = config.get('voice', '')  # 提取角色名称
        self.skip_tts_keywords = config.get('skip_tts_keywords', [])  # 跳过TTS的关键词
        self.tts_probability = config.get('tts_probability', 100)  # TTS转换概率
        self.speed = config.get('speed', 1.0)  # 音频播放速度
        self.gain = config.get('gain', 0.0)  # 音频增益
        self.enabled = config.get('global_enabled', True)  # 从配置读取全局开关状态（与schema默认一致）
        # 文本预处理相关开关
        self.read_brackets = bool(config.get('read_brackets', True))  # 是否朗读括号中的内容
        self.filter_symbols_enabled = bool(config.get('filter_symbols_enabled', False))  # 是否过滤符号
        self.filter_symbols = config.get('filter_symbols', ["+", "-", "=", "/"])  # 需要过滤的符号列表
        self.reference_mode = bool(config.get('reference_mode', False))  # 参考模式：语音+原文
        self.debug_tts_input = bool(config.get('debug_tts_input', False))  # 调试：先发出完整的TTS输入文本
        self.only_llm_tts = bool(config.get('only_llm_tts', False))  # 仅对AI模型回复进行TTS
        # 新增：最大保存音频文件数量（0=不限制）
        self.max_saved_audios = int(config.get('max_saved_audios', 5))
        # 访问控制：模式 + 列表
        self.group_access_mode = self._normalize_access_mode(config.get('group_access_mode', 'disabled'))
        self.group_access_list = config.get('group_access_list', [])
        self.max_tts_chars = int(config.get('max_tts_chars', 0))  # 超过该长度跳过TTS，0为不限制
        # 规范化基础 URL，移除多余斜杠
        if isinstance(self.api_url, str):
            self.api_url = self.api_url.rstrip('/')
        # 规范化跳过关键词列表
        self.skip_tts_keywords = self._normalize_skip_keywords(self.skip_tts_keywords)
        # 简易去重缓存，避免同一会话短时间内重复合成
        self._recent_tts = {}
        self._dedup_ttl_seconds = 10
        # 使用插件数据目录存放输出音频，避免污染源代码目录
        try:
            self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_vits")
        except Exception:
            # 兜底：仍然使用源目录，但仅作为读取，不建议写入
            self.plugin_data_dir = Path(__file__).parent
        try:
            Path(self.plugin_data_dir).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        # 使用专用输出目录保存每次合成的音频，文件名带时间戳，避免覆盖与缓存
        self._tts_output_dir = Path(self.plugin_data_dir) / "tts"
        try:
            self._tts_output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self._tts_lock = asyncio.Lock()
        # 启动时清理历史文件，保证重载后策略仍然生效
        try:
            self._enforce_audio_retention()
        except Exception:
            pass

    @filter.on_llm_response()
    async def _cache_llm_response_text(self, event: AstrMessageEvent, response):
        """缓存原始 LLM 文本，供 TTS 使用，避免后续装饰插件改写。"""
        try:
            text = getattr(response, 'completion_text', '') or ''
            if text:
                try:
                    event.set_extra('vits_raw_text', text)
                    event.set_extra('vits_has_llm', True)
                except Exception:
                    pass
        except Exception:
            pass

    async def _build_tts_input(self, plain_text: str) -> str:
        """根据配置构造发送到 TTS 的 input 文本。"""
        # 若文本已自带以 <|endofprompt|> 结尾的指令前缀，则直接透传，避免重复添加
        try:
            # 仅识别标准形式：以 <|endofprompt|> 结尾的前缀
            if re.match(r"^\s*.*?<\|endofprompt\|>\s*", plain_text, flags=re.DOTALL):
                return plain_text
        except Exception:
            pass
        # 文本预处理：可选删除括号内容与配置中的符号
        text = plain_text

        # 1) 可选：删除圆括号和方括号中的内容（常用于旁白、注释）
        #    仅在关闭 read_brackets 时生效
        if not self.read_brackets:
            try:
                # 非贪婪匹配，删除中英文圆括号和方括号中的内容（包含括号本身）
                # 英文圆括号 ()
                text = re.sub(r"\([^\)]*\)", "", text)
                # 英文方括号 []
                text = re.sub(r"\[[^\]]*\]", "", text)
                # 中文圆括号 （）
                text = re.sub(r"（[^）]*）", "", text)
                # 中文方括号 【】
                text = re.sub(r"【[^】]*】", "", text)
            except Exception:
                pass

        # 2) 可选：过滤指定符号字符
        if self.filter_symbols_enabled:
            try:
                # 将配置项统一为字符串列表
                symbols = []
                raw_symbols = self.filter_symbols or []
                for s in raw_symbols:
                    try:
                        ch = str(s)
                    except Exception:
                        continue
                    if ch:
                        symbols.append(ch)

                if symbols:
                    # 为安全起见逐个替换，而不是拼 regex
                    for sym in symbols:
                        text = text.replace(sym, "")
            except Exception:
                pass

        # 插件不再添加前缀，直接透传预处理后的文本
        return text

    def _strip_end_marker_prefix_in_chain(self, result) -> None:
        """若文本开头包含任意以 <|endofprompt|> 结尾的前缀，则在消息链中剔除。"""
        try:
            if not result or not getattr(result, 'chain', None):
                return
            # 仅剥离标准 <|endofprompt|> 形式，保留其他类似标记
            pattern = re.compile(r"^.*?<\|endofprompt\|>\s*", re.DOTALL)
            new_chain = []
            stripped = False
            for comp in result.chain:
                if not stripped and isinstance(comp, Plain):
                    new_text = pattern.sub('', comp.text)
                    new_chain.append(Plain(new_text))
                    stripped = True
                else:
                    new_chain.append(comp)
            result.chain = new_chain
        except Exception:
            pass

    def _get_system_voices_dict(self):
        """预置系统音色，统一管理，保持插入顺序"""
        return {
            "alex": "沉稳男声",
            "benjamin": "低沉男声",
            "charles": "磁性男声",
            "david": "欢快男声",
            "anna": "沉稳女声",
            "bella": "激情女声",
            "claire": "温柔女声",
            "diana": "欢快女声",
        }

    def _save_global_enabled_state(self, enabled: bool):
        """保存全局启用状态到配置"""
        try:
            # 更新内存中的配置
            self.config['global_enabled'] = enabled
            if hasattr(self.context, 'save_config'):
                self.context.save_config(self.config)
                logger.info(f"已保存TTS全局开关状态: {enabled}")
            elif hasattr(self.context, 'update_config'):
                self.context.update_config('global_enabled', enabled)
                logger.info(f"已保存TTS全局开关状态: {enabled}")
            else:
                logger.warning("context 未提供保存配置的方法，'global_enabled' 状态变更不会持久化。")
        except Exception as e:
            logger.error(f"保存TTS开关状态失败: {e}")

    def _normalize_skip_keywords(self, keywords):
        """将 skip 关键词规范化为去空格小写列表；若为空，使用内置默认"""
        try:
            raw = keywords
            items = []
            if isinstance(raw, str):
                # 先按逗号分，再按空白分
                parts = []
                for seg in raw.split(','):
                    parts.extend(seg.split())
                items = parts
            elif isinstance(raw, (list, tuple, set)):
                items = list(raw)
            else:
                items = []

            normalized = []
            for it in items:
                try:
                    s = str(it).strip().lower()
                except Exception:
                    continue
                if s:
                    normalized.append(s)

            if not normalized:
                # 内置默认（含中英混合关键词）
                normalized = [
                    "astrbot", "llm", "http", "https", "www.", ".com", ".cn", "reset",
                    "链接", "网址", "入群", "退群", "涩图", "语音", "音色", "错误类型", "tts", "转换", "新对话", "服务提供商", "列表"
                ]

            return normalized
        except Exception:
            return [
                "astrbot", "llm", "http", "https", "www.", ".com", ".cn", "reset",
                "链接", "网址", "入群", "退群", "涩图", "语音", "音色", "错误类型", "tts", "转换", "新对话", "服务提供商", "列表"
            ]

    def _save_config_field(self, key: str, value):
        """保存单个配置字段到配置文件或由宿主框架持久化"""
        try:
            self.config[key] = value
            if hasattr(self.context, 'save_config'):
                self.context.save_config(self.config)
                logger.info(f"已保存配置项 {key} = {value}")
            elif hasattr(self.context, 'update_config'):
                self.context.update_config(key, value)
                logger.info(f"已保存配置项 {key} = {value}")
            else:
                logger.warning(f"context 未提供保存配置的方法，配置项 {key} 的变更不会持久化。")
        except Exception as e:
            logger.error(f"保存配置项失败 {key}: {e}")

    def _generate_unique_audio_paths(self):
        """生成本次合成专用的唯一文件路径（时间戳 + uuid）。"""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        except Exception:
            ts = str(int(time.time() * 1000))
        uid = uuid.uuid4().hex[:8]
        base = f"{ts}_{uid}"
        final_audio_path = (self._tts_output_dir / f"{base}.wav").resolve()
        tmp_audio_path = (self._tts_output_dir / f"{base}.tmp").resolve()
        return final_audio_path, tmp_audio_path

    def _enforce_audio_retention(self):
        """按配置最大数量保留音频文件；超过上限删除最早的，同时顺带清理残留的 .tmp。"""
        try:
            out_dir = getattr(self, '_tts_output_dir', None)
            if not out_dir or not Path(out_dir).exists():
                return
            # 仅管理正式的 .wav 文件
            wav_files = sorted(Path(out_dir).glob('*.wav'), key=lambda p: (p.stat().st_mtime, p.name))
            if isinstance(self.max_saved_audios, int) and self.max_saved_audios > 0:
                excess = len(wav_files) - self.max_saved_audios
                if excess > 0:
                    for p in wav_files[:excess]:
                        try:
                            p.unlink()
                        except Exception:
                            pass
            # 清理残留临时文件
            for tmp in Path(out_dir).glob('*.tmp'):
                try:
                    tmp.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.warning(f"清理历史音频失败: {e}")

    def _normalize_access_mode(self, value) -> str:
        """将配置中的访问模式归一化为内部标识：disabled/whitelist/blacklist。
        同时兼容中文选项：不限制/白名单/黑名单。
        """
        try:
            text = str(value).strip().lower()
        except Exception:
            return 'disabled'
        mapping = {
            'disabled': 'disabled',
            'whitelist': 'whitelist',
            'blacklist': 'blacklist',
            '不限制': 'disabled',
            '白名单': 'whitelist',
            '黑名单': 'blacklist',
        }
        # 直接命中英文
        if text in mapping:
            return mapping[text]
        # 中文原样匹配（lower 对中文无影响）
        if value in mapping:
            return mapping[value]
        return 'disabled'

    @filter.command("vits", priority=1)
    async def vits(self, event: AstrMessageEvent):
        """启用/禁用语音插件"""
        # 兼容不同平台获取用户名
        if hasattr(event, 'get_sender_name'):
            user_name = event.get_sender_name()
        elif hasattr(event, 'get_user_id'):
            user_name = str(event.get_user_id())
        else:
            user_name = "用户"
            
        self.enabled = not self.enabled
        
        # 保存状态到配置文件
        self._save_global_enabled_state(self.enabled)
        
        if self.enabled:
            yield event.plain_result(f"启用语音插件, {user_name} (已保存到配置)")
        else:
            yield event.plain_result(f"禁用语音插件, {user_name} (已保存到配置)")

    @filter.command("vitsre", priority=1)
    async def vits_restart(self, event: AstrMessageEvent):
        """重启语音插件：相当于执行两次 /vits（切换→再切换回）"""
        # 记录原始状态
        original_enabled = self.enabled
        # 第一次切换
        self.enabled = not original_enabled
        self._save_global_enabled_state(self.enabled)
        # 稍作等待，确保外部观察者有机会感知变更（可选）
        try:
            await asyncio.sleep(0.1)
        except Exception:
            pass
        # 第二次切换，恢复到原状态
        self.enabled = original_enabled
        self._save_global_enabled_state(self.enabled)
        yield event.plain_result(
            f"已重启语音插件，当前状态：{'启用' if self.enabled else '禁用'}（配置已同步）"
        )

    @filter.command("voices", priority=1)
    async def vits_voices(self, event: AstrMessageEvent):
        """查看所有可用的音色列表"""
        try:
            # 获取用户自定义音色列表
            custom_voices = []
            try:
                url = f"{self.api_url}/audio/voice/list"
                headers = {"Authorization": f"Bearer {self.api_key}"}
                
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as response:
                        if response.status == 200:
                            response_text = await response.text()
                            voice_list = json.loads(response_text)
                            
                            # 尝试多种可能的数据结构
                            if voice_list and isinstance(voice_list, dict):
                                if 'data' in voice_list:
                                    custom_voices = voice_list['data']
                                elif 'result' in voice_list:
                                    custom_voices = voice_list['result']
                                elif 'voices' in voice_list:
                                    custom_voices = voice_list['voices']
                                elif 'items' in voice_list:
                                    custom_voices = voice_list['items']
                                elif isinstance(voice_list, list):
                                    custom_voices = [voice_list] if voice_list else []
                                else:
                                    custom_voices = [voice_list] if voice_list else []
                            elif isinstance(voice_list, list):
                                custom_voices = voice_list
                                
            except Exception as e:
                logger.warning(f"获取自定义音色列表失败: {e}")
            
            # 构建音色信息
            voice_info = "可用音色列表\n"
            voice_info += "=" * 20 + "\n\n"
            
            # 系统预置音色
            voice_info += "系统预置音色：\n"
            system_voices = self._get_system_voices_dict()
            for voice_id, voice_desc in system_voices.items():
                voice_info += f"• {voice_id} - {voice_desc}\n"
                voice_info += f"  {self.api_name}:{voice_id}\n\n"
            
            # 用户自定义音色
            if custom_voices and len(custom_voices) > 0:
                voice_info += "用户自定义音色：\n"
                for voice in custom_voices:
                    if isinstance(voice, dict):
                        voice_name = voice.get('name', voice.get('customName', '未知'))
                        voice_uri = voice.get('uri', voice.get('id', '未知'))
                        
                        voice_info += f"• {voice_name}\n"
                        voice_info += f"  {voice_uri}\n\n"
                    else:
                        voice_info += f"• {str(voice)}\n\n"
            else:
                voice_info += "用户自定义音色：暂无\n"
                voice_info += "如需使用自定义音色，请先在硅基流动平台上传音频文件\n\n"
            
            voice_info += "使用说明：\n"
            voice_info += "1. 系统预置音色：在配置中设置 voice 为 '模型名:音色名'\n"
            voice_info += "2. 自定义音色：在配置中设置 voice 为完整的 URI\n"
            voice_info += f"3. 当前配置：模型={self.api_name}, 音色={self.api_voice}\n"
            voice_info += "4. 使用/voice <音色名> 快速切换预置/自定义音色\n"
            
            yield event.plain_result(voice_info)
            
        except Exception as e:
            logger.error(f"获取音色列表失败: {e}")
            yield event.plain_result(f"获取音色列表失败：{str(e)}")

    @filter.command("voice", priority=1)
    async def change_voice(self, event: AstrMessageEvent):
        """快速切换音色"""
        # 获取命令参数
        message_text = event.get_message_str().strip()
        parts = message_text.split()
        
        if len(parts) < 2:
            # 显示当前音色和使用说明
            current_voice = self.api_voice if self.api_voice else "未设置"
            help_text = f"当前音色：{current_voice}\n\n"
            help_text += "使用方法：/voice <音色名>\n\n"
            help_text += "可用的系统预置音色：\n"
            help_text += "• alex - 沉稳男声\n"
            help_text += "• benjamin - 低沉男声\n" 
            help_text += "• charles - 磁性男声\n"
            help_text += "• david - 欢快男声\n"
            help_text += "• anna - 沉稳女声\n"
            help_text += "• bella - 激情女声\n"
            help_text += "• claire - 温柔女声\n"
            help_text += "• diana - 欢快女声\n\n"
            help_text += "示例：/voice alex"
            yield event.plain_result(help_text)
            return
        
        voice_name = parts[1]  # 保持原始大小写，因为自定义音色可能区分大小写
        voice_name_lower = voice_name.lower()
        
        # 预定义的系统音色
        system_voices = self._get_system_voices_dict()
        
        # 获取用户自定义音色列表
        custom_voices = {}
        try:
            url = f"{self.api_url}/audio/voice/list"
            headers = {"Authorization": f"Bearer {self.api_key}"}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        response_text = await response.text()
                        voice_list = json.loads(response_text)
                        
                        # 解析自定义音色数据
                        if voice_list and isinstance(voice_list, dict):
                            if 'data' in voice_list:
                                voices_data = voice_list['data']
                            elif 'result' in voice_list:
                                voices_data = voice_list['result']
                            elif 'voices' in voice_list:
                                voices_data = voice_list['voices']
                            elif 'items' in voice_list:
                                voices_data = voice_list['items']
                            else:
                                voices_data = voice_list if isinstance(voice_list, list) else []
                        elif isinstance(voice_list, list):
                            voices_data = voice_list
                        else:
                            voices_data = []
                        
                        # 构建自定义音色字典
                        for voice in voices_data:
                            if isinstance(voice, dict):
                                voice_name_key = voice.get('name', voice.get('customName', ''))
                                voice_uri = voice.get('uri', voice.get('id', ''))
                                if voice_name_key and voice_uri:
                                    custom_voices[voice_name_key] = voice_uri
        except Exception as e:
            logger.warning(f"获取自定义音色列表失败: {e}")
        
        # 检查是否是系统预置音色
        if voice_name_lower in system_voices:
            # 构建新的音色配置
            new_voice = f"{self.api_name}:{voice_name_lower}"
            self.api_voice = new_voice
            # 持久化
            self._save_config_field('voice', new_voice)
            
            voice_desc = system_voices[voice_name_lower]
            yield event.plain_result(f"已切换到系统音色：{voice_name_lower} ({voice_desc})\n配置：{new_voice}")
        
        # 检查是否是自定义音色
        elif voice_name in custom_voices:
            # 使用自定义音色的完整URI
            new_voice = custom_voices[voice_name]
            self.api_voice = new_voice
            # 持久化
            self._save_config_field('voice', new_voice)
            
            yield event.plain_result(f"已切换到自定义音色：{voice_name}\n配置：{new_voice}")
        
        else:
            # 不支持的音色
            all_system_voices = ", ".join(system_voices.keys())
            all_custom_voices = ", ".join(custom_voices.keys()) if custom_voices else "无"
            
            error_msg = f"不支持的音色：{voice_name}\n\n"
            error_msg += f"可用系统音色：{all_system_voices}\n"
            error_msg += f"可用自定义音色：{all_custom_voices}"
            
            yield event.plain_result(error_msg)

    @filter.command("vits%", priority=1)
    async def set_tts_probability(self, event: AstrMessageEvent):
        """设置TTS转换概率"""
        # 获取命令参数
        message_text = event.get_message_str().strip()
        parts = message_text.split()
        
        if len(parts) < 2:
            # 显示当前概率设置
            # 为帮助信息创建简化版本，避免关键词触发跳过逻辑
            help_text = f"当前TTS转换概率：{self.tts_probability}%\n\n"
            help_text += "使用方法：/vits% <概率值>\n\n"
            help_text += "示例：\n"
            help_text += "/vits% 50  # 设置50%概率\n"
            help_text += "/vits% 100 # 设置100%概率（每次都转换）\n" 
            help_text += "/vits% 0   # 设置0%概率（从不转换）"
            yield event.plain_result(help_text)
            return
        
        try:
            new_probability = int(parts[1])
            
            # 验证概率值范围
            if new_probability < 0 or new_probability > 100:
                yield event.plain_result("概率值必须在0-100之间！\n\n0表示从不转换，100表示每次都转换。")
                return
            
            # 更新概率设置
            self.tts_probability = new_probability
            # 持久化
            self._save_config_field('tts_probability', new_probability)
            
            if new_probability == 0:
                yield event.plain_result("已设置TTS转换概率为0%，将不会进行语音转换。")
            elif new_probability == 100:
                yield event.plain_result("已设置TTS转换概率为100%，将每次都进行语音转换。")
            else:
                yield event.plain_result(f"已设置TTS转换概率为{new_probability}%，大约{new_probability}%的消息会转换为语音。")
                
        except ValueError:
            yield event.plain_result("请输入有效的数字！\n\n示例：/vits% 50")

    @filter.command("speed", priority=1)
    async def set_speed(self, event: AstrMessageEvent):
        """设置音频播放速度"""
        # 获取命令参数
        message_text = event.get_message_str().strip()
        parts = message_text.split()
        
        if len(parts) < 2:
            # 显示当前速度设置
            yield event.plain_result(f"当前音频播放速度：{self.speed}\n\n使用方法：/speed <速度值>\n\n示例：\n/speed 1.0  # 正常速度\n/speed 1.5  # 1.5倍速\n/speed 0.5  # 0.5倍速\n\n有效范围：0.25 - 4.0")
            return
        
        try:
            new_speed = float(parts[1])
            
            # 验证速度值范围
            if new_speed < 0.25 or new_speed > 4.0:
                yield event.plain_result("速度值必须在0.25-4.0之间！\n\n0.25表示最慢，4.0表示最快，1.0为正常速度。")
                return
            
            # 更新速度设置
            self.speed = new_speed
            # 持久化
            self._save_config_field('speed', new_speed)
            
            if new_speed == 1.0:
                yield event.plain_result("已设置音频播放速度为正常速度（1.0倍）。")
            elif new_speed < 1.0:
                yield event.plain_result(f"已设置音频播放速度为{new_speed}倍，语音将变慢。")
            else:
                yield event.plain_result(f"已设置音频播放速度为{new_speed}倍，语音将变快。")
                
        except ValueError:
            yield event.plain_result("请输入有效的数字！\n\n示例：/speed 1.5")

    @filter.command("gain", priority=1)
    async def set_gain(self, event: AstrMessageEvent):
        """设置音频增益"""
        # 获取命令参数
        message_text = event.get_message_str().strip()
        parts = message_text.split()
        
        if len(parts) < 2:
            # 显示当前增益设置
            yield event.plain_result(f"当前音频增益：{self.gain}dB\n\n使用方法：/gain <增益值>\n\n示例：\n/gain 0    # 默认音量\n/gain 3    # 增加3dB（更响）\n/gain -3   # 减少3dB（更轻）\n\n有效范围：-10 到 10 dB")
            return
        
        try:
            new_gain = float(parts[1])
            
            # 验证增益值范围
            if new_gain < -10 or new_gain > 10:
                yield event.plain_result("增益值必须在-10到10之间！\n\n负值表示降低音量，正值表示提高音量，0为默认音量。")
                return
            
            # 更新增益设置
            self.gain = new_gain
            # 持久化
            self._save_config_field('gain', new_gain)
            
            if new_gain == 0.0:
                yield event.plain_result("已设置音频增益为默认值（0dB）。")
            elif new_gain < 0:
                yield event.plain_result(f"已设置音频增益为{new_gain}dB，音量将降低。")
            else:
                yield event.plain_result(f"已设置音频增益为{new_gain}dB，音量将提高。")
                
        except ValueError:
            yield event.plain_result("请输入有效的数字！\n\n示例：/gain 3")

    @filter.command("vitsinfo", priority=1)
    async def vits_info(self, event: AstrMessageEvent):
        """查看插件当前配置信息"""
        info_text = f"VITS插件配置信息：\n"
        info_text += f"状态：{'启用' if self.enabled else '禁用'}\n"
        info_text += f"全局开关配置：{'启用' if self.config.get('global_enabled', True) else '禁用'}\n"
        info_text += f"音色：{self.api_voice}\n"
        info_text += f"播放速度：{self.speed}\n"
        info_text += f"音频增益：{self.gain}dB\n"
        info_text += f"转换概率：{self.tts_probability}%\n"
        info_text += f"最大TTS字符：{self.max_tts_chars if self.max_tts_chars > 0 else '不限制'}\n"
        info_text += f"跳过关键词：{', '.join(self.skip_tts_keywords)}\n"
        info_text += f"仅对AI模型TTS：{'开启' if self.only_llm_tts else '关闭'}\n\n"
        info_text += "说明：状态显示当前运行状态，全局开关配置显示重启后的默认状态"
        yield event.plain_result(info_text)

    async def _create_speech_request(self, tts_input_text: str, output_audio_path: Path):
        """创建语音合成请求"""
        try:
            # 构建请求数据
            request_data = {
                "model": self.api_name,
                "input": tts_input_text,
                "response_format": "wav"
            }
            
            # 添加音色参数
            if self.api_voice:
                request_data["voice"] = self.api_voice
            
            # 添加speed和gain参数
            if self.speed != 1.0:
                request_data["speed"] = self.speed
            if self.gain != 0.0:
                request_data["gain"] = self.gain
            
            # 设置请求头
            headers = {
                'Content-Type': 'application/json',
                'Authorization': f"Bearer {self.api_key}"
            }
            
            # 使用aiohttp发送请求
            url = f"{self.api_url}/audio/speech"
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=request_data, headers=headers) as response:
                    if response.status == 200:
                        # 将响应内容写入文件
                        with open(output_audio_path, 'wb') as f:
                            async for chunk in response.content.iter_chunked(8192):
                                f.write(chunk)
                        return True
                    else:
                        error_text = await response.text()
                        raise Exception(f"API请求失败，状态码: {response.status}, 错误信息: {error_text}")
                
        except Exception as e:
            logger.error(f"语音转换失败: {e}")
            raise e

    async def _should_skip_tts(self, text: str) -> bool:
        """检查是否应该跳过TTS转换"""
        # 长度阈值检查
        if isinstance(self.max_tts_chars, int) and self.max_tts_chars > 0 and len(text) > self.max_tts_chars:
            return True
        # 检测是否包含跳过TTS的关键词
        text_lower = text.lower()
        for keyword in self.skip_tts_keywords:
            if keyword in text_lower:
                return True
        
        # 概率检测：根据设置的概率决定是否进行TTS转换
        if self.tts_probability < 100:
            # 生成1-100之间的随机数
            random_num = random.randint(1, 100)
            if random_num > self.tts_probability:
                return True
        
        return False

    def _is_duplicate_request(self, session_key: str, text: str) -> bool:
        """检查并标记重复请求，避免短时间内相同文本重复TTS"""
        try:
            import time
            now = time.time()
            # 清理过期项
            if len(self._recent_tts) > 256:
                to_delete = []
                for k, ts in self._recent_tts.items():
                    if now - ts > self._dedup_ttl_seconds:
                        to_delete.append(k)
                for k in to_delete:
                    self._recent_tts.pop(k, None)

            # 使用稳定哈希降低碰撞概率
            try:
                digest = hashlib.sha1(text.encode('utf-8')).hexdigest()
            except Exception:
                digest = str(hash(text))
            key = f"{session_key}:{digest}"
            ts = self._recent_tts.get(key)
            if ts and (now - ts) <= self._dedup_ttl_seconds:
                return True
            self._recent_tts[key] = now
            return False
        except Exception:
            return False

    async def _convert_to_speech(self, event: AstrMessageEvent, result, session_key: str):
        """将文本结果转换为语音"""
        # 初始化plain_text变量
        plain_text = ""
        chain = result.chain

        # 遍历组件
        # 如果结果中已经存在语音记录，则不再进行二次转换
        try:
            for comp in chain:
                if isinstance(comp, Record):
                    return
        except Exception:
            pass

        for comp in result.chain:
            # 图片 / @ / 回复 等场景跳过语音
            if isinstance(comp, (Image, At, AtAll, Reply)):
                return  # 静默退出，不添加错误提示
            if isinstance(comp, Plain):
                # 不再过滤字符，保持文本原样，避免误删 TTS 控制标记
                plain_text += comp.text

        # 清理首尾空白并校验是否为空
        plain_text = plain_text.strip()
        if not plain_text:
            return

        # 去重：同一会话短时间内相同文本不重复合成
        if self._is_duplicate_request(session_key, plain_text):
            return

        # 检查是否应该跳过TTS
        if await self._should_skip_tts(plain_text):
            # 若文本前部包含以 <|endofprompt|> 结尾的提示前缀，剔除后再以文字发送
            self._strip_end_marker_prefix_in_chain(result)
            return

        # 为本次请求生成唯一输出文件，使用临时文件 + 原子替换，并加锁避免并发冲突
        final_audio_path, tmp_audio_path = self._generate_unique_audio_paths()

        try:
            async with self._tts_lock:
                # 构造用于TTS的输入文本（保留可能的人设前缀）
                # 优先使用 on_llm_response 缓存的原始文本，避免被其他插件改写
                src_text = plain_text
                try:
                    cached = event.get_extra('vits_raw_text')
                    if isinstance(cached, str) and cached.strip():
                        src_text = cached
                except Exception:
                    pass
                tts_input = await self._build_tts_input(src_text)
                # 调试：先发送完整的TTS输入文本
                if self.debug_tts_input:
                    try:
                        preview_text = tts_input
                        # 展示时可限制长度以免过长
                        if len(preview_text) > 4000:
                            preview_text = preview_text[:4000] + "..."
                        result.chain = [Plain(preview_text)]
                    except Exception:
                        pass
                success = await self._create_speech_request(tts_input, tmp_audio_path)
            if success:
                # 原子替换到最终文件（尽量同卷内替换，失败则回退为复制）
                try:
                    os.replace(tmp_audio_path, final_audio_path)
                except Exception:
                    try:
                        data = Path(tmp_audio_path).read_bytes()
                        Path(final_audio_path).write_bytes(data)
                        try:
                            os.remove(tmp_audio_path)
                        except Exception:
                            pass
                    except Exception:
                        raise
                if self.reference_mode or self.debug_tts_input:
                    # 参考模式：语音 + 原文本（剔除可能存在的前缀）
                    # 复制原文本
                    original_text = ''
                    try:
                        text_builder = []
                        for comp in result.chain:
                            if isinstance(comp, Plain):
                                text_builder.append(comp.text)
                        original_text = '\n'.join([t for t in text_builder if t]).strip()
                    except Exception:
                        original_text = ''
                    # 剔除前缀
                    try:
                        if original_text:
                            # 仅匹配标准形式：<|endofprompt|> 后的文本
                            original_text = re.sub(r"^.*?<\|endofprompt\|>\s*", '', original_text, flags=re.DOTALL)
                    except Exception:
                        pass
                    # 组合为：语音 + 文本
                    new_chain = [Record(file=str(final_audio_path))]
                    if original_text:
                        new_chain.append(Plain(original_text))
                    result.chain = new_chain
                else:
                    # 仅发送语音
                    result.chain = [Record(file=str(final_audio_path))]
                try:
                    event.set_extra('vits_sent', True)
                except Exception:
                    pass
                # 成功后执行一次目录清理，重载不影响
                try:
                    self._enforce_audio_retention()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"语音转换失败: {e}")
            chain.append(Plain(f"语音转换失败：{str(e)}"))

    @filter.command("ttsmax", priority=1)
    async def set_max_saved_audios_cmd(self, event: AstrMessageEvent):
        """设置最大保存音频文件数量（0=不限制）。用法：/ttsmax <数量>。"""
        msg = event.get_message_str().strip()
        parts = msg.split()
        if len(parts) < 2:
            yield event.plain_result(
                f"当前最大保存音频数量：{self.max_saved_audios}（0=不限制）\n用法：/ttsmax 200"
            )
            return
        try:
            val = int(parts[1])
            if val < 0 or val > 10000:
                yield event.plain_result("数量需在 0~10000 之间（0=不限制）")
                return
            self.max_saved_audios = val
            self._save_config_field('max_saved_audios', val)
            try:
                self._enforce_audio_retention()
            except Exception:
                pass
            yield event.plain_result(f"已设置最大保存音频数量为：{val}（0=不限制）")
        except ValueError:
            yield event.plain_result("请输入有效数字，例如：/ttsmax 200")

    @filter.on_decorating_result(priority=-100)
    async def on_decorating_result(self, event: AstrMessageEvent):
        # 插件是否启用
        if not self.enabled:
            # 在停用语音时仍然清理可见文本中以 <|endofprompt|> 结尾的前缀
            try:
                result = event.get_result()
                if result is not None:
                    self._strip_end_marker_prefix_in_chain(result)
            except Exception:
                pass
            return

        # 会话访问控制：disabled/whitelist/blacklist
        try:
            mode = (self.group_access_mode or 'disabled').lower()
            if mode not in ['disabled', 'whitelist', 'blacklist']:
                mode = 'disabled'
            acl = {str(x).strip() for x in (self.group_access_list or []) if str(x).strip() != ''}
            group_id = event.get_group_id() if hasattr(event, 'get_group_id') else None
            sender_id = event.get_sender_id() if hasattr(event, 'get_sender_id') else None
            ctx_id = str(group_id).strip() if group_id else (str(sender_id).strip() if sender_id else '')

            if mode == 'whitelist':
                # 仅名单内启用
                if ctx_id == '' or ctx_id not in acl:
                    result = event.get_result()
                    if result is not None:
                        self._strip_end_marker_prefix_in_chain(result)
                    return
            elif mode == 'blacklist':
                # 名单内禁用
                if ctx_id != '' and ctx_id in acl:
                    result = event.get_result()
                    if result is not None:
                        self._strip_end_marker_prefix_in_chain(result)
                    return
        except Exception:
            # 任何异常都不应阻断正常流程
            pass
        try:
            if event.get_extra('vits_processed'):
                if event.get_extra('vits_sent'):
                    event.clear_result()
                return
            event.set_extra('vits_processed', True)
        except Exception:
            pass

        # 获取事件结果
        result = event.get_result()
        if result is None:
            return
        # 若启用仅LLM TTS，则没有 LLM 文本的消息不做语音
        try:
            if self.only_llm_tts and not event.get_extra('vits_has_llm'):
                self._strip_end_marker_prefix_in_chain(result)
                return
        except Exception:
            pass
        # 传递会话键，用于去重
        session_key = getattr(event, 'unified_msg_origin', None) or event.get_session_id()
        await self._convert_to_speech(event, result, session_key)
