import re
import json
import os
import asyncio
from datetime import datetime, timedelta
import dateparser
from collections import deque

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import AstrBotConfig 
import astrbot.api.message_components as Comp

# --- HTML Templates (unchanged) ---
RANKING_TMPL = """
<!DOCTYPE html>
<html>
<head>
<style>
    body { margin: 0; font-family: sans-serif; display: flex; justify-content: center; align-items: center; padding: 20px; box-sizing: border-box; background-color: #f8f9fa; }
    .container { background: white; border-radius: 12px; padding: 25px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); min-width: 550px; }
    h1 { color: #333; border-bottom: 2px solid #f1f3f5; padding-bottom: 15px; margin-top: 0; font-size: 24px; }
    ol { list-style-type: none; padding-left: 0; margin-bottom: 0; }
    li { background: #f8f9fa; margin-bottom: 10px; padding: 15px; border-radius: 8px; font-size: 18px; display: flex; align-items: center; }
    .rank { font-weight: bold; color: #ff9800; min-width: 60px; }
    .name { font-weight: bold; color: #0056b3; flex-grow: 1; }
    .count { color: #28a745; }
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
    body { margin: 0; font-family: sans-serif; display: flex; justify-content: center; align-items: center; padding: 20px; box-sizing: border-box; background-color: #f8f9fa; }
    .container { background: white; border-radius: 12px; padding: 25px; box-shadow: 0 4px 12px rgba(0,0,0,0.08); min-width: 550px; }
    h1 { color: #333; border-bottom: 2px solid #f1f3f5; padding-bottom: 15px; margin-top: 0; font-size: 24px; }
    ul { list-style-type: none; padding-left: 0; margin-bottom: 0; }
    li { border-left: 4px solid #007bff; background: #f8f9fa; margin-bottom: 10px; padding: 15px; font-size: 16px; }
    .status { font-weight: bold; margin-right: 10px; }
    .status-done { color: #28a745; }
    .status-pending { color: #ffc107; }
</style>
</head>
<body><div class="container"><h1>【{{ user_name }}】的承诺列表</h1><ul>{% for p in promises %}<li><span class="status {{ 'status-done' if p.done else 'status-pending' }}">{{ p.status }}</span>{{ p.content }}</li>{% endfor %}</ul></div></body>
</html>
"""

# --- Constants ---
DATA_DIR = os.path.join("data", "promise_keeper")
PROMISES_FILE = os.path.join(DATA_DIR, "promises.json")
REGEX_CONFIG_FILE = os.path.join(os.path.dirname(__file__), "regex_config.json")

@register("PromiseKeeper", "YourName", "自动记录聊天中的承诺并到期提醒", "2.1.0") # LLM 上下文优化
class PromiseKeeperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config; self.promises_data = {}; self.regex_rules = []; self.fuzzy_mappings = {}; self.time_of_day_mappings = {}
        self.message_history = {}

        os.makedirs(DATA_DIR, exist_ok=True)
        
        self._load_promises()
        self._load_plugin_configs()
        
        self.reminder_task = asyncio.create_task(self._promise_reminder_task())
        self.llm_analysis_task = asyncio.create_task(self._llm_batch_analysis_task())
        logger.info("言而有信：提醒任务和LLM分析任务已启动。")

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
                self.time_of_day_mappings = config_data.get("time_of_day_mappings", {"早上": 9, "中午": 12, "晚上": 20})
                self.regex_rules = config_data.get("rules", [])
            logger.info(f"言而有信：成功加载 {len(self.regex_rules)} 条正则和所有时间映射。")
        except Exception as e: logger.error(f"言而有信：加载插件配置失败: {e}", exc_info=True)

    def _parse_time_to_timestamp(self, text: str) -> float:
        if text in self.fuzzy_mappings:
            duration_minutes = self.fuzzy_mappings[text]; dt_obj = datetime.now() + timedelta(minutes=duration_minutes); return dt_obj.timestamp()
        target_hour = None; date_text = text
        for tod_keyword, hour in self.time_of_day_mappings.items():
            if tod_keyword in text:
                target_hour = hour; date_text = text.replace(tod_keyword, '').strip(); break
        match = re.match(r"第(\d+|二|三|四|五|六|七|八|九|十)天", date_text)
        if match:
            day_str = match.group(1); day_map = {"二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
            days = day_map.get(day_str, int(day_str) if day_str.isdigit() else 1)
            dt_obj = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days)
        else:
            dt_obj = dateparser.parse(date_text, settings={'PREFER_DATES_FROM': 'future'})
        if not dt_obj: return 0.0
        if target_hour is not None: dt_obj = dt_obj.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        return dt_obj.timestamp()

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
                        at_user = Comp.At(qq=promise['user_id']); reminder_text = Comp.Plain(f" 提醒一下，你之前承诺的 “{promise['content']}” 时间已经过半咯！")
                        await self.context.send_message(promise['unified_msg_origin'], MessageChain([at_user, reminder_text]))
                        promise['halfway_reminded'] = True; needs_saving = True
                if needs_saving: self._save_promises()
            except Exception as e: logger.error(f"言而有信：提醒任务出错: {e}", exc_info=True)

    def _clean_llm_json_output(self, llm_output: str) -> str:
        match = re.search(r"```json\s*([\s\S]+?)\s*```", llm_output)
        if match: return match.group(1).strip()
        return llm_output.strip().strip('`')

    # --- 核心改动 1: 升级 LLM 分析任务 ---
    async def _llm_batch_analysis_task(self):
        while True:
            await asyncio.sleep(600)
            provider_id = self.config.get("llm_provider_id")
            if not provider_id:
                logger.debug("言而有信：未在配置中指定llm_provider_id，跳过LLM分析。")
                continue

            provider = self.context.get_provider_by_id(provider_id)
            if not provider:
                logger.error(f"言而有信：找不到ID为 '{provider_id}' 的LLM提供商。")
                continue

            promises_to_analyze = []
            for user_id, promises in self.promises_data.items():
                for i, promise in enumerate(promises):
                    if not promise.get("llm_analyzed", False):
                        # 直接使用承诺中存储的上下文快照
                        context_snapshot = promise.get("context_snapshot", [])
                        
                        promises_to_analyze.append({
                            "user_id": user_id, "promise_index": i,
                            "original_content": promise["content"],
                            "original_time_text": promise["deadline_text"],
                            # 使用快照，不再从全局历史记录中获取
                            "context": context_snapshot 
                        })

            if not promises_to_analyze:
                logger.info("言而有信：没有需要LLM分析的新承诺。")
                continue

            logger.info(f"言而有信：准备将 {len(promises_to_analyze)} 条承诺提交给LLM进行分析...")
            system_prompt = """
            你是一个精准的“承诺分析师”。你的任务是分析一个JSON数组，其中每个对象都包含一条用户承诺及其上下文（context）。
            你需要为每一条承诺，结合上下文，精确地提取或修正其核心“承诺内容”（corrected_content）和“承诺时间”（corrected_time_text）。
            - “承诺内容”应具体、简洁。
            - “承诺时间”应从上下文中推断，如果原文已经很明确，则直接使用。
            请严格按照JSON格式返回一个包含所有分析结果的数组，即使你认为某些承诺无需修改，也要原样返回。
            返回的JSON数组中，每个对象必须包含 "user_id", "promise_index", "corrected_content", "corrected_time_text" 四个字段。
            """
            
            try:
                llm_response = await provider.text_chat(prompt=json.dumps(promises_to_analyze, ensure_ascii=False, indent=2), system_prompt=system_prompt)
                cleaned_json_str = self._clean_llm_json_output(llm_response.completion_text)
                analysis_results = json.loads(cleaned_json_str)
                
                needs_saving = False
                for result in analysis_results:
                    uid = result.get("user_id"); p_idx = result.get("promise_index")
                    if uid in self.promises_data and isinstance(p_idx, int) and p_idx < len(self.promises_data[uid]):
                        p_to_update = self.promises_data[uid][p_idx]
                        p_to_update["content"] = result["corrected_content"]
                        p_to_update["deadline_text"] = result["corrected_time_text"]
                        new_ts = self._parse_time_to_timestamp(result["corrected_time_text"])
                        if new_ts: p_to_update["due_timestamp"] = new_ts
                        p_to_update["llm_analyzed"] = True; needs_saving = True
                
                if needs_saving: self._save_promises()
                logger.info(f"言而有信：LLM分析完成，成功更新 {len(analysis_results)} 条承诺。")
            except Exception as e:
                logger.error(f"言而有信：LLM批量分析过程中出错: {e}", exc_info=True)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        history_key = event.unified_msg_origin
        if history_key not in self.message_history:
            self.message_history[history_key] = deque(maxlen=200)
        
        message_text = event.message_str.strip()
        self.message_history[history_key].append(f"{event.get_sender_name()}: {message_text}")
        
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

    # --- 核心改动 2: 升级记录函数 ---
    def _record_promise(self, event: AstrMessageEvent, content: str, deadline_text: str, due_timestamp: float):
        user_id_str = str(event.get_sender_id())
        
        # 获取当前会话的聊天记录
        history_key = event.unified_msg_origin
        current_history = list(self.message_history.get(history_key, []))
        
        # 截取最后的5条作为上下文快照 (包括当前这条承诺)
        context_snapshot = current_history[-5:]

        record = {
            "content": content, "deadline_text": deadline_text, "due_timestamp": due_timestamp,
            "made_timestamp": datetime.now().timestamp(), "user_name": event.get_sender_name(),
            "user_id": user_id_str, "unified_msg_origin": event.unified_msg_origin,
            "reminded": False, "halfway_reminded": False, "llm_analyzed": False,
            # 将精确的上下文快照存入记录
            "context_snapshot": context_snapshot
        }
        if user_id_str not in self.promises_data: self.promises_data[user_id_str] = []
        self.promises_data[user_id_str].append(record); self._save_promises()

    async def _render_with_retry(self, template, data, max_retries=2):
        for attempt in range(max_retries + 1):
            try:
                return await self.html_render(template, data)
            except Exception as e:
                logger.error(f"言而有信：图片渲染失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}")
                if attempt >= max_retries: raise e 
                await asyncio.sleep(1)

    @filter.command("言而有信排行")
    async def promise_leaderboard(self, event: AstrMessageEvent):
        try:
            if not self.promises_data: yield event.plain_result("目前还没有任何人的承诺记录哦。"); return
            user_counts = {p[0]['user_name']: len(p) for p in self.promises_data.values() if p}
            if not user_counts: yield event.plain_result("目前还没有任何人的承诺记录哦。"); return
            sorted_users = sorted(user_counts.items(), key=lambda item: item[1], reverse=True)
            template_data = {"users": [{"rank": i + 1, "name": name, "count": count} for i, (name, count) in enumerate(sorted_users[:10])]}
            image_url = await self._render_with_retry(RANKING_TMPL, template_data)
            yield event.image_result(image_url)
        except Exception as e:
            yield event.plain_result(f"图片生成失败，渲染服务可能出现问题。错误: {e}")

    @filter.command("言而有信")
    async def check_user_promises(self, event: AstrMessageEvent):
        try:
            target_id, target_name = None, None
            for msg_component in event.message_obj.message:
                if isinstance(msg_component, Comp.At):
                    target_id = str(msg_component.qq); target_name = (self.promises_data.get(target_id) or [{}])[0].get('user_name', f'用户{target_id}'); break
            if not target_id:
                target_id = str(event.get_sender_id()); target_name = event.get_sender_name()
            user_promises = self.promises_data.get(target_id)
            if not user_promises: yield event.plain_result(f"没有找到 {target_name} 的承诺记录。"); return
            template_data = {"user_name": target_name, "promises": []}
            for p in user_promises:
                is_done = p.get('reminded', False); status_text = "✅(已提醒)" if is_done else f"⏳(截止于{datetime.fromtimestamp(p['due_timestamp']).strftime('%m-%d %H:%M')})"
                template_data["promises"].append({"status": status_text, "content": p['content'], "done": is_done})
            image_url = await self._render_with_retry(USER_PROMISES_TMPL, template_data)
            yield event.image_result(image_url)
        except Exception as e:
            yield event.plain_result(f"图片生成失败，渲染服务可能出现问题。错误: {e}")

    async def terminate(self):
        if self.reminder_task: self.reminder_task.cancel()
        if self.llm_analysis_task: self.llm_analysis_task.cancel()
        logger.info("言而有信：提醒和LLM分析任务已取消。")
