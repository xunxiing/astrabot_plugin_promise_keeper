import re
import json
import os
import asyncio
from datetime import datetime

# --- 核心改动：导入您的 PromiseDetector ---
from .promise_detector.predictor import PromiseDetector

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig 
import astrbot.api.message_components as Comp

# --- HTML 模板简化 (移除截止状态) ---
RANKING_TMPL = """
<!DOCTYPE html>
<html>
<head>
<style>
    body { font-family: sans-serif; margin: 0; padding: 0; background-color: #f9f9f9; }
    .container { background: white; padding: 20px; width: 600px; box-sizing: border-box; }
    h1 { color: #333; border-bottom: 2px solid #eee; padding-bottom: 10px; margin-top:0; }
    ol { list-style-type: none; padding-left: 0; }
    li { background: #f0f4f8; margin-bottom: 8px; padding: 15px; border-radius: 5px; font-size: 18px; display: flex; align-items: center; }
    .rank { font-weight: bold; color: #ff9800; min-width: 60px; }
    .name { font-weight: bold; color: #0056b3; flex-grow: 1; }
    .count { color: #4CAF50; }
</style>
</head>
<body><div class="container"><h1>🏆 【言而有信】承诺排行榜</h1><ol>{% for user in users %}<li><span class="rank">Top {{ user.rank }}</span><span class="name">{{ user.name }}</span><span class="count">{{ user.count }} 次</span></li>{% endfor %}</ol></div></body>
</html>
"""

USER_PROMISES_TMPL = """
<!DOCTYPE html>
<html>
<head>
<style>
    body { font-family: sans-serif; margin: 0; padding: 0; background-color: #f9f9f9; }
    .container { background: white; padding: 20px; width: 600px; box-sizing: border-box; }
    h1 { color: #333; border-bottom: 2px solid #eee; padding-bottom: 10px; margin-top:0; }
    ul { list-style-type: none; padding-left: 0; }
    li { border-left: 4px solid #007bff; background: #f8f9fa; margin-bottom: 8px; padding: 15px; font-size: 16px; }
</style>
</head>
<body><div class="container"><h1>【{{ user_name }}】的承诺列表</h1><ul>{% for p in promises %}<li>{{ p.content }}</li>{% endfor %}</ul></div></body>
</html>
"""

# --- 常量定义 ---
DATA_DIR = os.path.join("data", "promise_keeper")
PROMISES_FILE = os.path.join(DATA_DIR, "promises.json")

@register("PromiseKeeperAI", "YourName", "由AI驱动的承诺记录器", "2.0.0") # 全新版本
class PromiseKeeperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.promises_data = {}
        # --- NEW: 初始化 PromiseDetector ---
        self.detector = None
        try:
            # 插件启动时，仅初始化一次模型
            logger.info("言而有信[AI]: 正在初始化承诺检测模型...")
            self.detector = PromiseDetector()
            logger.info("言而有信[AI]: 模型初始化成功！")
        except IOError as e:
            logger.error(f"言而有信[AI]: 模型加载失败！请确保模型文件已放置在 'promise_detector/models' 目录下。错误: {e}")
        except Exception as e:
            logger.error(f"言而有信[AI]: 初始化时发生未知错误: {e}", exc_info=True)

        os.makedirs(DATA_DIR, exist_ok=True)
        self._load_promises()
        # --- REMOVED: 不再需要后台提醒任务 ---

    def _load_promises(self):
        try:
            if os.path.exists(PROMISES_FILE):
                with open(PROMISES_FILE, 'r', encoding='utf-8') as f: self.promises_data = json.load(f)
                logger.info("言而有信[AI]: 成功加载历史承诺。")
        except Exception as e: logger.error(f"言而有信[AI]: 加载承诺失败: {e}", exc_info=True)

    def _save_promises(self):
        try:
            with open(PROMISES_FILE, 'w', encoding='utf-8') as f: json.dump(self.promises_data, f, ensure_ascii=False, indent=4)
        except Exception as e: logger.error(f"言而有信[AI]: 保存承诺失败: {e}", exc_info=True)

    # --- REMOVED: _load_plugin_configs, _parse_time_to_timestamp, _promise_reminder_task ---
    
    # --- REFACTORED: on_all_message 使用 AI 模型 ---
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        # 如果模型加载失败，则不执行任何操作
        if not self.detector:
            return

        message_text = event.message_str.strip()
        if not message_text:
            return

        # 我们仍然可以利用上下文，虽然您的示例中没有，但模型支持
        # 这里我们简化为无上下文预测，您可以根据需要改回
        prediction = self.detector.predict(text=message_text)
        
        logger.debug(f"言而有信[AI] 预测: '{message_text}' -> {prediction}")

        if prediction["label_name"] == "Promise" and prediction["confidence"] > 0.86: # 可配置的置信度阈值
            user_id_str = str(event.get_sender_id())
            # 检查是否重复记录完全相同的承诺
            if any(p['content'] == message_text for p in self.promises_data.get(user_id_str, [])):
                return
            
            self._record_promise(event, message_text)
            yield event.plain_result(f"【言而有信】AI已记录你的承诺 (置信度: {prediction['confidence']:.2%})")

    # --- REFACTORED: _record_promise 简化版 ---
    def _record_promise(self, event: AstrMessageEvent, content: str):
        user_id_str = str(event.get_sender_id())
        
        # 记录的字段大大简化
        record = {
            "content": content,
            "made_timestamp": datetime.now().timestamp(),
            "user_name": event.get_sender_name(),
            "user_id": user_id_str
        }
        
        if user_id_str not in self.promises_data:
            self.promises_data[user_id_str] = []
        
        self.promises_data[user_id_str].append(record)
        self._save_promises()

    # --- 排行榜指令 (逻辑不变) ---
    @filter.command("言而有信排行")
    async def promise_leaderboard(self, event: AstrMessageEvent):
        # ... 此函数逻辑与之前版本基本相同 ...
        if not self.promises_data: yield event.plain_result("目前还没有任何人的承诺记录哦。"); return
        user_counts = {p[0]['user_name']: len(p) for p in self.promises_data.values() if p}
        if not user_counts: yield event.plain_result("目前还没有任何人的承诺记录哦。"); return
        sorted_users = sorted(user_counts.items(), key=lambda item: item[1], reverse=True)
        template_data = {"users": [{"rank": i + 1, "name": name, "count": count} for i, (name, count) in enumerate(sorted_users[:10])]}
        image_url = await self.html_render(RANKING_TMPL, template_data); yield event.image_result(image_url)

    # --- 个人查询指令 (简化版) ---
    @filter.command("言而有信")
    async def check_user_promises(self, event: AstrMessageEvent):
        # ... 此函数逻辑与之前版本基本相同，但模板数据简化了 ...
        target_id, target_name = None, None
        for msg_component in event.message_obj.message:
            if isinstance(msg_component, Comp.At):
                target_id = str(msg_component.qq)
                target_name = (self.promises_data.get(target_id) or [{}])[0].get('user_name', f'用户{target_id}')
                break
        
        if not target_id:
            target_id = str(event.get_sender_id()); target_name = event.get_sender_name()

        user_promises = self.promises_data.get(target_id)
        if not user_promises: yield event.plain_result(f"没有找到 {target_name} 的承诺记录。"); return
        
        # 模板数据大大简化，不再有状态
        template_data = {"user_name": target_name, "promises": user_promises}
        image_url = await self.html_render(USER_PROMISES_TMPL, template_data)
        yield event.image_result(image_url)

    async def terminate(self):
        # 不再需要取消后台任务
        logger.info("言而有信[AI] 插件已卸载。")