"""
自动领取奖励回归（全部用桩，不需要真实客户端）：

  A. ClaimRewardsService：三类来源（lol-rewards / lol-missions / lol-event-hub）
     的查询、过滤、请求体构造、节流、开关实时生效、失败只记一次日志
  B. claim_once_async：手动领取不依赖 run()/自动化开关，能正常跑一轮并汇报

运行： python _test_claim_rewards.py
"""
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

from scripts.claim_rewards import ClaimRewardsService, _loc_name

PASS = []


def check(name, cond):
    PASS.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name)


# ============================================================ 桩 LCU
class StubLCU:
    """按 endpoint 返回预制数据，记录所有 POST/PUT 调用。"""

    def __init__(self, grants=None, missions=None, events=None,
                 connected=True, can_connect=True, fail_on=()):
        self.connected = connected
        self.can_connect = can_connect
        self.grants = grants if grants is not None else []
        self.missions = missions if missions is not None else []
        self.events = events if events is not None else []
        self.fail_on = set(fail_on)          # 这些端点返回失败
        self.posts = []                      # (endpoint, json_body)
        self.puts = []

    def is_connected(self):
        return self.connected

    def connect(self):
        if self.can_connect:
            self.connected = True
            return True
        return False

    def get_json(self, endpoint):
        if endpoint.startswith("/lol-rewards/v1/grants"):
            return self.grants
        if endpoint == "/lol-missions/v1/missions":
            return self.missions
        if endpoint == "/lol-event-hub/v1/events":
            return self.events
        return None

    def post_ok(self, endpoint, json_body=None):
        self.posts.append((endpoint, json_body))
        return endpoint not in self.fail_on

    def put_ok(self, endpoint, json_body=None):
        self.puts.append((endpoint, json_body))
        return endpoint not in self.fail_on


GRANTS = [
    {   # 单选 1/1
        "info": {"id": "grant-1", "status": "PENDING_SELECTION"},
        "rewardGroup": {
            "id": "group-1",
            "localizations": {"title": "冠军胶囊"},
            "rewards": [{"id": "r1"}, {"id": "r2"}],
            "selectionStrategyConfig": {"maxSelectionsAllowed": 1},
        },
    },
    {   # 多选一：最多选 2
        "info": {"id": "grant-2", "status": "PENDING_SELECTION"},
        "rewardGroup": {
            "id": "group-2",
            "localizations": {},
            "rewards": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
            "selectionStrategyConfig": {"maxSelectionsAllowed": 2},
        },
    },
]

MISSIONS = [
    {   # 待领取：选 1 个奖励组
        "id": "mission-1", "status": "SELECT_REWARDS", "title": "每日任务",
        "rewards": [{"rewardGroup": "g1", "description": "宝箱"},
                    {"rewardGroup": "g2", "description": "钥匙"}],
        "rewardStrategy": {"selectMaxGroupCount": 1},
    },
    {   # 已完成但不用选 → 跳过
        "id": "mission-2", "status": "COMPLETED", "title": "旧任务",
        "rewards": [{"rewardGroup": "gx"}],
    },
]

EVENTS = [
    {"eventId": "evt-1",
     "eventInfo": {"eventName": "春节活动", "unclaimedRewardCount": 3}},
    {"eventId": "evt-2",
     "eventInfo": {"eventName": "无奖励活动", "unclaimedRewardCount": 0}},
]


# ============================================================ A. 三类来源
def test_claim_pass():
    print("[A] ClaimRewardsService.claim_pass")

    logs = []
    lcu = StubLCU(grants=GRANTS, missions=MISSIONS, events=EVENTS)
    svc = ClaimRewardsService(lcu, get_flags=lambda: {"auto_claim": True},
                              on_event=logs.append)

    n = svc.claim_pass()
    check("三类全部领取（2 奖励 + 1 任务 + 1 事件 = 4）", n == 4)

    # ① 奖励：请求体与端点
    sel = [(e, b) for e, b in lcu.posts if e.endswith("/select")]
    check("两个待领奖励都发起 select", len(sel) == 2)
    _, body = sel[0]
    check("select 请求体字段正确",
          body == {"grantId": "grant-1", "rewardGroupId": "group-1",
                   "selections": ["r1"]})
    check("多选一按 maxSelectionsAllowed 取前 N 个",
          sel[1][1]["selections"] == ["a", "b"])

    # ② 任务：只处理 SELECT_REWARDS
    check("只处理 SELECT_REWARDS 任务", len(lcu.puts) == 1)
    ep, body = lcu.puts[0]
    check("任务领取端点为 /player/{id}",
          ep == "/lol-missions/v1/player/mission-1")
    check("任务领取按 selectMaxGroupCount 选奖励组",
          body == {"rewardGroups": ["g1"]})

    # ③ 事件中心：只领 unclaimedRewardCount > 0
    claim_all = [e for e, _ in lcu.posts if "claim-all" in e]
    check("只领取有待领奖励的事件",
          claim_all == ["/lol-event-hub/v1/events/evt-1/reward-track/claim-all"])

    check("每项领取都有日志",
          sum("🎁" in m for m in logs) == 4)


def test_throttle_and_flag():
    print("[B] 节流与开关")

    # 开关关闭：tick 不动作
    lcu = StubLCU(grants=GRANTS, missions=MISSIONS, events=EVENTS)
    svc = ClaimRewardsService(lcu, get_flags=lambda: {"auto_claim": False})
    svc._tick()
    check("auto_claim 关闭时 tick 不领取", not lcu.posts and not lcu.puts)

    # 开关打开：tick 立即跑一轮
    flags = {"auto_claim": True}
    lcu2 = StubLCU(grants=GRANTS, missions=MISSIONS, events=EVENTS)
    svc2 = ClaimRewardsService(lcu2, get_flags=lambda: flags)
    svc2._tick()
    check("auto_claim 开启时 tick 立即领取", len(lcu2.posts) == 3)

    # 30s 节流：紧接着再 tick 不重复
    svc2._tick()
    check("30s 节流内不重复领取", len(lcu2.posts) == 3)

    # 手动领取占位后，tick 也不会紧接着重跑
    lcu3 = StubLCU(grants=GRANTS, missions=MISSIONS, events=EVENTS)
    svc3 = ClaimRewardsService(lcu3, get_flags=lambda: {"auto_claim": True})
    svc3.claim_once_async()
    deadline = time.time() + 3
    while time.time() < deadline and len(lcu3.posts) < 3:
        time.sleep(0.05)
    svc3._tick()
    check("手动领取后 tick 不重叠重复", len(lcu3.posts) == 3)


def test_failures_logged_once():
    print("[C] 失败路径")

    logs = []
    lcu = StubLCU(grants=GRANTS, missions=MISSIONS, events=EVENTS,
                  fail_on=("/lol-rewards/v1/grants/grant-1/select",))
    svc = ClaimRewardsService(lcu, get_flags=lambda: {"auto_claim": True},
                              on_event=logs.append)
    svc.claim_pass()
    svc.claim_pass()
    warns = [m for m in logs if "领取失败" in m]
    check("失败只播报一次（不刷屏）", len(warns) == 1)
    check("失败不阻塞其他来源领取",
          len(lcu.posts) + len(lcu.puts) >= 3)


def test_claim_once_async():
    print("[D] 手动领取")

    logs = []
    lcu = StubLCU(grants=GRANTS, missions=MISSIONS, events=EVENTS)
    # 不启动 run()，模拟自动化关闭时点「🎁 领取奖励」
    svc = ClaimRewardsService(lcu, on_event=logs.append)
    svc.claim_once_async()
    deadline = time.time() + 3
    while time.time() < deadline and "领取完成" not in "".join(logs):
        time.sleep(0.05)
    check("未启动 run() 也能手动领取一轮", len(lcu.posts) == 3)
    check("手动领取汇报结果",
          any("领取完成（本轮 4 项）" in m for m in logs))

    # 未连接且连不上时如实提示
    logs2 = []
    lcu2 = StubLCU(connected=False, can_connect=False)
    svc2 = ClaimRewardsService(lcu2, on_event=logs2.append)
    svc2.claim_once_async()
    deadline = time.time() + 3
    while time.time() < deadline and not any("未连接" in m for m in logs2):
        time.sleep(0.05)
    check("未连接时提示登录客户端",
          any("未连接" in m for m in logs2))


def test_loc_name():
    print("[E] localizations 兼容解析")
    check("title 优先", _loc_name({"title": "胶囊", "en_US": "Capsule"}) == "胶囊")
    check("按语言分键取任意值", _loc_name({"en_US": "Capsule"}) == "Capsule")
    check("空 dict 返回 None", _loc_name({}) is None)
    check("None 返回 None", _loc_name(None) is None)


if __name__ == "__main__":
    t0 = time.time()
    test_claim_pass()
    test_throttle_and_flag()
    test_failures_logged_once()
    test_claim_once_async()
    test_loc_name()
    bad = [n for n, ok in PASS if not ok]
    print(f"\n{'=' * 46}")
    print(f"  通过 {len(PASS) - len(bad)}/{len(PASS)}  ({time.time() - t0:.1f}s)")
    if bad:
        print("  失败:")
        for n in bad:
            print(f"    ❌ {n}")
        sys.exit(1)
