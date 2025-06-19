import re
import json
import os
import asyncio
from datetime import datetime, timedelta
import dateparser

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig 
import astrbot.api.message_components as Comp

# --- HTML 模板定义 ---
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
<body>
    <div class="container">
        <h1>🏆 【言而有信】承诺排行榜</h1>
        <ol>
            {% for user in users %}
            <li>
                <span class="rank">Top {{ user.rank }}</span>
                <span class="name">{{ user.name }}</span>
                <span class="count">{{ user.count }} 次</span>
            </li>
            {% endfor %}
        </ol>
    </div>
</body>
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
<body>
    <div class="container">
        <h1>【{{ user_name }}】的承诺列表</h1>
        <ul>
            {% for p in promises %}
            <li>
                <span class="status {{ 'status-done' if p.done else 'status-pending' }}">{{ p.status }}</span>
                {{ p.content }}
            </li>
            {% endfor %}
        </ul>
    </div>
</body>
</html>
"""

# --- 常量定义 ---
DATA_DIR = os.path.join("data", "promise_keeper")
PROMISES_FILE = os.path.join(DATA_DIR, "promises.json")
REGEX_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "regex_config.json")


@register("PromiseKeeper", "YourName", "自动记录聊天中的承诺并到期提醒", "1.7.2") # 修复Web UI兼容性
class PromiseKeeperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config; self.promises_data = {}; self.regex_rules = []; self.fuzzy_mappings = {}
        os.makedirs(DATA_DIR, exist_ok=True)
        self._load_promises(); self._load_plugin_configs()
        self.reminder_task = asyncio.create_task(self._promise_reminder_task())
        logger.info("言而有信：后台提醒任务已启动。")

    def _load_promises(self):
        try:
            if os.path.exists(PROMISES_FILE):
                with open(PROMISES_FILE, 'r', encoding='utf-8') as f: self.promises_data = json.load(f)
                logger.info("言而有信：成功加载历史承诺。")
        except Exception as e: logger.error(f"言而有信：加载承诺失败: {e}", exc_info=True)

    def _save_promises(self):
        try:
            with open(PROMISES_FILE, 'w', encoding='utf-8') as f: json.dump(self.promises_data, f, ensure_ascii=False, indent=4)
        except Exception as e: logger.error(f"言而有信：保存承诺失败: {e}", exc_info=True)

    def _load_plugin_configs(self):
        try:
            with open(REGEX_CONFIG_FILE, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
                self.fuzzy_mappings = config_data.get("fuzzy_time_mappings", {"马上": 30, "现在": 5})
                self.regex_rules = config_data.get("rules", [])
            logger.info(f"言而有信：成功加载 {len(self.regex_rules)} 条正则和模糊词映射。")
        except Exception as e: logger.error(f"言而有信：加载插件配置失败: {e}", exc_info=True)

    def _parse_time_to_timestamp(self, text: str) -> float:
        if text in self.fuzzy_mappings:
            duration_minutes = self.fuzzy_mappings[text]; dt_obj = datetime.now() + timedelta(minutes=duration_minutes); return dt_obj.timestamp()
        
        match = re.match(r"第(\d+|二|三|四|五|六|七|八|九|十)天", text)
        if match:
            day_str = match.group(1); day_map = {"二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
            days = day_map.get(day_str, int(day_str) if day_str.isdigit() else 1)
            dt_obj = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days); return dt_obj.timestamp()

        dt_obj = dateparser.parse(text, settings={'PREFER_DATES_FROM': 'future'})
        if dt_obj: return dt_obj.timestamp()
        return 0.0
        
    async def _promise_reminder_task(self):
        while True:
            await asyncio.sleep(60)
            try:
                now_ts = datetime.now().timestamp(); needs_saving = False
                promises_to_process = [p for promises in self.promises_data.values() for p in promises]

                for promise in promises_to_process:
                    if not promise.get('reminded', False) and promise['due_timestamp'] <= now_ts:
                        at_user = Comp.At(qq=promise['user_id']); reminder_text = Comp.Plain(f" 喂！你之前承诺的 “{promise['content']}” 时间到啦！")
                        await self.context.send_message(promise['unified_msg_origin'], MessageChain([at_user, reminder_text]))
                        promise['reminded'] = True; needs_saving = True; continue

                    total_duration = promise['due_timestamp'] - promise['made_timestamp']
                    if total_duration < 60: continue
                    halfway_point_ts = promise['made_timestamp'] + total_duration / 2
                    if not promise.get('halfway_reminded', False) and now_ts >= halfway_point_ts:
                        at_user = Comp.At(qq=promise['user_id']); reminder_text = Comp.Plain(f" 提醒一下，你承诺的 “{promise['content']}” 时间已经过半咯！")
                        await self.context.send_message(promise['unified_msg_origin'], MessageChain([at_user, reminder_text]))
                        promise['halfway_reminded'] = True; needs_saving = True

                if needs_saving: self._save_promises()
            except Exception as e: logger.error(f"言而有信：提醒任务出错: {e}", exc_info=True)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        message_text = event.message_str.strip()
        if not message_text: return
        user_id_str = str(event.get_sender_id())
        for rule in self.regex_rules:
            match = re.search(rule['pattern'], message_text)
            if match:
                captured = match.groupdict(); time_text = captured.get('time','').strip(); action_text = captured.get('action','').strip()
                if not time_text or not action_text: continue
                if any(p['content'] == action_text for p in self.promises_data.get(user_id_str, [])): continue
                due_ts = self._parse_time_to_timestamp(time_text)
                if not due_ts: continue
                self._record_promise(event, action_text, time_text, due_ts)
                due_dt_str = datetime.fromtimestamp(due_ts).strftime('%Y-%m-%d %H:%M:%S')
                yield event.plain_result(f"【言而有信】已记录承诺：{action_text}\n预计完成时间：{due_dt_str}"); return

    def _record_promise(self, event: AstrMessageEvent, content: str, deadline_text: str, due_timestamp: float):
        user_id_str = str(event.get_sender_id())
        record = {
            "content": content, "deadline_text": deadline_text, "due_timestamp": due_timestamp,
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
        
        # 核心修正：使用 event.message_obj.message
        for msg_component in event.message_obj.message:
            if isinstance(msg_component, Comp.At):
                target_id = str(msg_component.qq)
                target_name = (self.promises_data.get(target_id) or [{}])[0].get('user_name', f'用户{target_id}')
                break
        
        if not target_id:
            target_id = str(event.get_sender_id()); target_name = event.get_sender_name()

        user_promises = self.promises_data.get(target_id)
        if not user_promises: yield event.plain_result(f"没有找到 {target_name} 的承诺记录。"); return
        
        template_data = {"user_name": target_name, "promises": []}
        for p in user_promises:
            is_done = p.get('reminded', False)
            status_text = "✅(已提醒)" if is_done else f"⏳(截止于{datetime.fromtimestamp(p['due_timestamp']).strftime('%m-%d %H:%M')})"
            template_data["promises"].append({"status": status_text, "content": p['content'], "done": is_done})
            
        image_url = await self.html_render(USER_PROMISES_TMPL, template_data)
        yield event.image_result(image_url)

    async def terminate(self):
        if self.reminder_task: self.reminder_task.cancel()
        logger.info("言而有信：后台提醒任务已取消。")