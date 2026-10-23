"""
自动领取奖励 (LCU only，无注入、无键鼠模拟)

实现内容 (对标 LeagueAkari claim-tools，与 Mac 版 xiaobai助手 v0.7.0 同源):
1. 奖励 (lol-rewards)：海克斯宝箱 / 胶囊 / 神秘图标等
   - GET  /lol-rewards/v1/grants?status=PENDING_SELECTION
   - POST /lol-rewards/v1/grants/{grantId}/select
     body {"grantId", "rewardGroupId", "selections": [奖励id...]}
   - 多选一按 rewardGroup.selectionStrategyConfig.maxSelectionsAllowed 取前 N 个
     （LeagueAkari 用等权重随机 ChoiceMaker；这里取前 N 个，结果确定可预期）

2. 任务奖励 (lol-missions)
   - GET  /lol-missions/v1/missions        筛 status == "SELECT_REWARDS"
   - PUT  /lol-missions/v1/player/{missionId}
     body {"rewardGroups": [奖励组id...]}
   - 数量按 mission.rewardStrategy.selectMaxGroupCount

3. 事件中心 / 通行证奖励轨 (lol-event-hub)
   - GET  /lol-event-hub/v1/events         筛 eventInfo.unclaimedRewardCount > 0
   - POST /lol-event-hub/v1/events/{eventId}/reward-track/claim-all   一键全领

设计约束（照搬 autoflow.py）:
  · 30 分钟节流（CLAIM_INTERVAL=1800s）；首次连接立即跑一轮，之后每 30 分钟一次；所有动作幂等 + 冷却，绝不刷接口
  · 开关实时生效：每轮 tick 都从 get_flags() 重新读，UI 一改立刻生效
  · 任何异常都吞掉并只记一次日志，不让自动化线程把界面拖死
  · claim_once_async() 供 UI「立即领取」：另起线程跑一轮，不阻塞界面，
    也不依赖自动化开关（引擎没启动也能用）
"""
from __future__ import annotations

import threading
import time

# 待领取状态常量（与 LeagueAkari 同源）
GRANT_STATUS_PENDING = "PENDING_SELECTION"
MISSION_STATUS_SELECT_REWARDS = "SELECT_REWARDS"


def _loc_name(loc):
    """localizations 字段兼容解析。

    不同客户端版本可能是 {"title": "..."}、按语言分键 {"zh_CN": "..."} 或空 dict，
    统一按字典处理，取不到返回 None。
    """
    if not isinstance(loc, dict) or not loc:
        return None
    for key in ("title", "zh_CN"):
        v = loc.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for v in loc.values():
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


class ClaimRewardsService(threading.Thread):
    """随引擎启停：自动领取 奖励 / 任务 / 事件中心 三类待领奖励。"""

    TICK = 0.5
    CLAIM_INTERVAL = 1800.0      # 自动检查节流（秒）= 30 分钟；首次连接立即跑一轮，之后每 30 分钟

    def __init__(self, lcu, get_flags=None, on_event=None):
        super().__init__(daemon=True)
        self.lcu = lcu
        self.get_flags = get_flags or (lambda: {})
        self.on_event = on_event
        self.running = True
        self._last_claim = 0.0
        self._warned = set()
        self._claim_lock = threading.Lock()   # 手动与自动轮不重叠

    # ---------- 对外 ----------

    def stop(self):
        self.running = False

    def flag(self, key, default=False):
        try:
            return bool(self.get_flags().get(key, default))
        except Exception:
            return default

    def claim_once_async(self):
        """UI「立即领取」：另起线程跑一轮，不阻塞界面。

        先占位 _last_claim，避免自动轮紧接着重复跑一遍。
        """
        with self._claim_lock:
            self._last_claim = time.time()

        def _worker():
            with self._claim_lock:
                try:
                    if not self.lcu.is_connected() and not self.lcu.connect():
                        self._log("⚠ 未连接客户端，无法领取（请先登录英雄联盟）")
                        return
                    n = self.claim_pass()
                    self._log(f"🎁 领取完成（本轮 {n} 项）" if n
                              else "暂无可领取的奖励")
                except Exception as e:
                    self._log(f"⚠ 领取奖励出错：{e}")

        threading.Thread(target=_worker, daemon=True).start()

    # ---------- 主循环 ----------

    def run(self):
        while self.running:
            try:
                self._tick()
            except Exception:
                pass
            time.sleep(self.TICK)

    def _tick(self):
        if not self.lcu or not self.lcu.is_connected():
            return
        if not self.flag("auto_claim", True):
            return
        now = time.time()
        if now - self._last_claim < self.CLAIM_INTERVAL:
            return
        with self._claim_lock:
            if now - self._last_claim < self.CLAIM_INTERVAL:
                return
            self._last_claim = now
            try:
                self.claim_pass()
            except Exception:
                pass

    # ---------- 领取主流程 ----------

    def claim_pass(self) -> int:
        """跑一轮三类来源，返回成功领取条数。"""
        n = 0
        n += self._claim_reward_grants()
        n += self._claim_missions()
        n += self._claim_event_hub()
        return n

    # ① 奖励（宝箱 / 胶囊 / 图标等）
    def _claim_reward_grants(self) -> int:
        grants = self.lcu.get_json(
            f"/lol-rewards/v1/grants?status={GRANT_STATUS_PENDING}")
        if not isinstance(grants, list):
            return 0
        count = 0
        for g in grants:
            try:
                if not isinstance(g, dict):
                    continue
                info = g.get("info") or {}
                group = g.get("rewardGroup") or {}
                grant_id = info.get("id")
                group_id = group.get("id")
                rewards = group.get("rewards") or []
                if not grant_id or not group_id or not rewards:
                    continue
                sel_cfg = group.get("selectionStrategyConfig") or {}
                try:
                    max_sel = int(sel_cfg.get("maxSelectionsAllowed") or 1)
                except (TypeError, ValueError):
                    max_sel = 1
                n = max(1, min(max_sel, len(rewards)))
                ids = [r.get("id") for r in rewards[:n] if r.get("id")]
                if not ids:
                    continue
                ok = self.lcu.post_ok(
                    f"/lol-rewards/v1/grants/{grant_id}/select",
                    json_body={
                        "grantId": grant_id,
                        "rewardGroupId": group_id,
                        "selections": ids,
                    })
                if ok:
                    count += 1
                    name = _loc_name(group.get("localizations")) or "奖励"
                    self._log(f"🎁 已领取：{name}")
                else:
                    self._log_once("claim-reward-fail",
                                   "⚠ 有奖励领取失败（可能需要手动选择），稍后自动重试")
            except Exception:
                continue
        return count

    # ② 任务奖励
    def _claim_missions(self) -> int:
        missions = self.lcu.get_json("/lol-missions/v1/missions")
        if not isinstance(missions, list):
            return 0
        count = 0
        for m in missions:
            try:
                if not isinstance(m, dict):
                    continue
                if m.get("status") != MISSION_STATUS_SELECT_REWARDS:
                    continue
                mid = m.get("id")
                rewards = m.get("rewards") or []
                if not mid or not rewards:
                    continue
                strategy = m.get("rewardStrategy") or {}
                try:
                    want = int(strategy.get("selectMaxGroupCount") or 1)
                except (TypeError, ValueError):
                    want = 1
                n = max(1, min(want, len(rewards)))
                groups = [r.get("rewardGroup") for r in rewards[:n]
                          if r.get("rewardGroup")]
                if not groups:
                    continue
                ok = self.lcu.put_ok(
                    f"/lol-missions/v1/player/{mid}",
                    json_body={"rewardGroups": groups})
                if ok:
                    count += 1
                    first = rewards[0] if isinstance(rewards[0], dict) else {}
                    name = m.get("title") or first.get("description") or "任务奖励"
                    self._log(f"🎁 已领取任务奖励：{name}")
                else:
                    self._log_once("claim-mission-fail",
                                   "⚠ 有任务奖励领取失败，稍后自动重试")
            except Exception:
                continue
        return count

    # ③ 事件中心 / 通行证奖励轨
    def _claim_event_hub(self) -> int:
        events = self.lcu.get_json("/lol-event-hub/v1/events")
        if not isinstance(events, list):
            return 0
        count = 0
        for e in events:
            try:
                if not isinstance(e, dict):
                    continue
                info = e.get("eventInfo") or {}
                try:
                    unclaimed = int(info.get("unclaimedRewardCount") or 0)
                except (TypeError, ValueError):
                    unclaimed = 0
                event_id = e.get("eventId")
                if not event_id or unclaimed <= 0:
                    continue
                ok = self.lcu.post_ok(
                    f"/lol-event-hub/v1/events/{event_id}/reward-track/claim-all")
                if ok:
                    count += 1
                    name = info.get("eventName") or event_id
                    self._log(f"🎁 已领取事件奖励：{name}")
                else:
                    self._log_once("claim-event-fail",
                                   "⚠ 事件中心奖励领取失败，稍后自动重试")
            except Exception:
                continue
        return count

    # ---------- 日志 ----------

    def _log(self, msg):
        if not self.on_event:
            return
        try:
            self.on_event(msg)
        except Exception:
            pass

    def _log_once(self, key, msg):
        if key in self._warned:
            return
        self._warned.add(key)
        self._log(msg)
