import re
import json
import os
import asyncio
from datetime import datetime, timedelta
import dateparser
from collections import deque
from .promise_detector.predictor import PromiseDetector

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig 
import astrbot.api.message_components as Comp

# --- HTML 模板 (无变化) ---
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
    .status { font-weight: bold; margin-right: 10px; }
    .status-done { color: #28a745; }
    .status-pending { color: #ffc107; }
</style>
</head>
<body><div class="container"><h1>【{{ user_name }}】的承诺列表</h1><ul>{% for p in promises %}<li><span class="status {{ 'status-done' if p.done else 'status-pending' }}">{{ p.status }}</span>{{ p.content }}</li>{% endfor %}</ul></div></body>
</html>
"""

# --- 常量定义 ---
DATA_DIR = os.path.join("data", "promise_keeper_ai")
PROMISES_FILE = os.path.join(DATA_DIR, "promises.json")

@register("PromiseKeeperAI", "YourName", "由AI驱动的承诺记录器", "2.1.1") # 修复LLM JSON解析
class PromiseKeeperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.promises_data = {}
        self.detector = None
        self._user_message_history = {}

        try:
            logger.info("言而有信[AI]: 正在初始化承诺检测模型...")
            self.detector = PromiseDetector()
            logger.info("言而有信[AI]: 模型初始化成功！")
        except Exception as e:
            logger.error(f"言而有信[AI]: 模型加载失败！请确保模型文件已放置在 'promise_detector/models' 目录下。错误: {e}", exc_info=True)

        os.makedirs(DATA_DIR, exist_ok=True)
        self._load_promises()
        self.reminder_task = asyncio.create_task(self._promise_reminder_task())
        logger.info("言而有信[AI]: 后台提醒任务已启动。")

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

    def _parse_time_to_timestamp(self, text: str) -> float:
        if not text or text.lower() == 'none' or '没有' in text: return 0.0
        dt_obj = dateparser.parse(text, settings={'PREFER_DATES_FROM': 'future'})
        return dt_obj.timestamp() if dt_obj else 0.0

    async def _promise_reminder_task(self):
        while True:
            await asyncio.sleep(60)
            try:
                now_ts = datetime.now().timestamp(); needs_saving = False
                all_promises = [p for promises in self.promises_data.values() for p in promises]

                for promise in all_promises:
                    if not promise.get('reminded', False) and promise.get('due_timestamp', 0) and promise['due_timestamp'] <= now_ts:
                        try:
                            at_user = Comp.At(qq=promise['user_id']); reminder_text = Comp.Plain(f" 喂！你之前承诺的 “{promise['content']}” 时间到啦！")
                            await self.context.send_message(promise['unified_origin'], MessageChain([at_user, reminder_text]))
                        except Exception as send_error:
                            logger.warning(f"言而有信[AI]: 发送到期提醒失败（用户可能已退群）: {send_error}")
                        promise['reminded'] = True; needs_saving = True; continue

                    total_duration = promise.get('due_timestamp', 0) - promise.get('made_timestamp', 0)
                    if total_duration < 120: continue
                    halfway_point_ts = promise['made_timestamp'] + total_duration / 2
                    if not promise.get('halfway_reminded', False) and now_ts >= halfway_point_ts:
                        try:
                            at_user = Comp.At(qq=promise['user_id']); reminder_text = Comp.Plain(f" 提醒一下，你承诺的 “{promise['content']}” 时间已经过半咯！")
                            await self.context.send_message(promise['unified_origin'], MessageChain([at_user, reminder_text]))
                        except Exception as send_error:
                            logger.warning(f"言而有信[AI]: 发送中点提醒失败（用户可能已退群）: {send_error}")
                        promise['halfway_reminded'] = True; needs_saving = True

                if needs_saving: self._save_promises()
            except Exception as e: logger.error(f"言而有信[AI]: 提醒任务出错: {e}", exc_info=True)
    
    # --- 核心 AI 管道 (JSON 清理逻辑) ---
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        if not self.detector: return

        message_text = event.message_str.strip()
        if not message_text: return
        user_id = event.get_sender_id()

        if user_id not in self._user_message_history:
            self._user_message_history[user_id] = deque(maxlen=7)
        self._user_message_history[user_id].append(message_text)
        
        history = list(self._user_message_history[user_id])
        nn_context = " ".join(history[-4:-1])
        
        prediction = self.detector.predict(text=message_text, context=nn_context)
        logger.debug(f"言而有信[NN] 预判: '{message_text}' -> {prediction}")
        
        if not (prediction["label_name"] == "Promise" and prediction["confidence"] > 0.8): return

        llm = self.context.get_using_provider()
        if not llm: logger.warning("言而有信[AI]: NN检测到可能承诺，但未找到可用LLM进行确认。"); return
            
        llm_context = "\n".join([f"历史消息{i+1}: {msg}" for i, msg in enumerate(history[:-1])])
        llm_prompt = f"当前消息: {history[-1]}"
        system_prompt = """
你是一个精准的“承诺分析”助手。请分析下面提供的聊天记录，判断“当前消息”是否构成一个需要被记录的承诺。
你的分析需要非常严格，忽略玩笑、比喻、或不明确的意图。
请严格按照以下JSON格式返回你的分析结果，不要添加任何额外的解释：
{
  "is_promise": boolean,
  "promise_content": string,
  "reminder_time": string
}
"""
        logger.debug(f"言而有信[LLM]: 准备调用LLM进行二次确认...")
        try:
            llm_response = await llm.text_chat(prompt=llm_prompt, system_prompt=system_prompt, contexts=[{"role": "user", "content": llm_context}])
            
            # --- 核心修正：清理并提取JSON字符串 ---
            raw_text = llm_response.completion_text
            json_str = raw_text
            
            # 查找第一个 '{' 和最后一个 '}' 来提取潜在的JSON
            if '```' in raw_text:
                start_index = raw_text.find('{')
                end_index = raw_text.rfind('}')
                if start_index != -1 and end_index != -1:
                    json_str = raw_text[start_index : end_index + 1]
                    logger.debug(f"言而有信[LLM]: 已从Markdown代码块中提取JSON: {json_str}")

            analysis = json.loads(json_str)
            # ------------------------------------
            
            logger.debug(f"言而有信[LLM] 分析结果: {analysis}")

            if analysis.get("is_promise"):
                content = analysis.get("promise_content")
                time_text = analysis.get("reminder_time")
                
                if not content: return # 如果LLM认为内容为空，则不记录
                
                if any(p['content'] == content for p in self.promises_data.get(str(user_id), [])): return

                due_ts = self._parse_time_to_timestamp(time_text)
                self._record_promise(event, content, due_ts)
                
                time_info = f"\n提醒时间：{time_text}" if due_ts else ""
                yield event.plain_result(f"【言而有信】AI已确认并记录承诺：\n内容：{content}{time_info}")

        except json.JSONDecodeError:
            logger.warning(f"言而有信[LLM]: 尝试解析清理后的字符串时，仍然发生JSON解码错误。原始返回: {llm_response.completion_text}")
        except Exception as e:
            logger.error(f"言而有信[AI]: LLM分析或后续处理时出错: {e}", exc_info=True)

    def _record_promise(self, event: AstrMessageEvent, content: str, due_timestamp: float):
        user_id_str = str(event.get_sender_id())
        record = {
            "content": content, "due_timestamp": due_timestamp,
            "made_timestamp": datetime.now().timestamp(), "user_name": event.get_sender_name(),
            "user_id": user_id_str, "unified_msg_origin": event.unified_msg_origin,
            "reminded": False, "halfway_reminded": False
        }
        if user_id_str not in self.promises_data: self.promises_data[user_id_str] = []
        self.promises_data[user_id_str].append(record); self._save_promises()

    @filter.command("言而有信排行")
    async def promise_leaderboard(self, event: AstrMessageEvent):
        if not self.promises_data: yield event.plain_result("目前还没有任何人的承诺记录哦。"); return
        user_counts = {p[0]['user_name']: len(p) for p in self.promises_data.values() if p}
        if not user_counts: yield event.plain_result("目前还没有任何人的承诺记录哦。"); return
        sorted_users = sorted(user_counts.items(), key=lambda item: item[1], reverse=True)
        template_data = {"users": [{"rank": i + 1, "name": name, "count": count} for i, (name, count) in enumerate(sorted_users[:10])]}
        image_url = await self.html_render(RANKING_TMPL, template_data); yield event.image_result(image_url)

    @filter.command("言而有信")
    async def check_user_promises(self, event: AstrMessageEvent):
        target_id, target_name = None, None
        for msg_component in event.message_obj.message:
            if isinstance(msg_component, Comp.At):
                target_id = str(msg_component.qq); target_name = (self.promises_data.get(target_id) or [{}])[0].get('user_name', f'用户{target_id}'); break
        if not target_id: target_id = str(event.get_sender_id()); target_name = event.get_sender_name()
        user_promises = self.promises_data.get(target_id)
        if not user_promises: yield event.plain_result(f"没有找到 {target_name} 的承诺记录。"); return
        template_data = {"user_name": target_name, "promises": []}
        for p in user_promises:
            is_done = p.get('reminded', False); due_ts = p.get('due_timestamp', 0)
            status_text = "✅(已提醒)" if is_done else (f"⏳(截止于{datetime.fromtimestamp(due_ts).strftime('%m-%d %H:%M')})" if due_ts else "📝(已记录)")
            template_data["promises"].append({"status": status_text, "content": p['content'], "done": is_done})
        image_url = await self.html_render(USER_PROMISES_TMPL, template_data); yield event.image_result(image_url)

    async def terminate(self):
        if self.reminder_task: self.reminder_task.cancel()
        logger.info("言而有信[AI] 插件已卸载。")