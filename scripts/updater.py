# --- START OF FILE updater.py ---
"""
英雄数据维护工具（纯官方数据源，无爬虫）

海克斯图鉴功能已移除，本工具仅保留英雄数据同步：
  1. sync_official_data()   从 Data Dragon 同步 champions.json（中英映射）
  2. update_pinyin_file()   由 champions.json 生成 pinyin_map.json（拼音首字母检索）
  3. download_from_github() 网络不通时，从 GitHub 仓库兜底下载上述两个数据文件

运行：python -m scripts.updater
"""
import json
import os
import sys

import requests

try:
    from scripts.config import DATA_DIR, CHAMPION_ID_FILE, PINYIN_FILE
except ImportError:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(current_dir)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    from config import DATA_DIR, CHAMPION_ID_FILE, PINYIN_FILE

# GitHub 仓库地址 (用于在线下载)
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/lixyd/xiaobai-helper/main"


# ================= 1. 官方英雄数据同步 =================
def sync_official_data():
    """从 Data Dragon 同步最新英雄中英映射，写入 champions.json。"""
    print(">>> 正在同步官方英雄数据...")
    try:
        ver_url = "https://ddragon.leagueoflegends.com/api/versions.json"
        version = requests.get(ver_url).json()[0]
        print(f"    当前游戏版本: {version}")

        champ_url = f"https://ddragon.leagueoflegends.com/cdn/{version}/data/zh_CN/champion.json"
        data = requests.get(champ_url).json()['data']

        official_en_to_cn = {}
        official_cn_to_en = {}
        for en_id, info in data.items():
            if en_id.startswith('Jade_'):
                continue
            cn_name = info['name']
            official_en_to_cn[en_id] = cn_name
            official_cn_to_en[cn_name] = en_id

        old_en_to_cn = {}
        if os.path.exists(CHAMPION_ID_FILE):
            with open(CHAMPION_ID_FILE, 'r', encoding='utf-8') as f:
                old_cn_to_en = json.load(f)
                old_en_to_cn = {en: cn for cn, en in old_cn_to_en.items()}

        with open(CHAMPION_ID_FILE, 'w', encoding='utf-8') as f:
            json.dump(official_cn_to_en, f, indent=4, ensure_ascii=False)

        new_champs = []
        renamed_champs = []
        for en_id, cn_name in official_en_to_cn.items():
            if en_id not in old_en_to_cn:
                new_champs.append(en_id)
            elif old_en_to_cn[en_id] != cn_name:
                renamed_champs.append(en_id)

        print(f"    同步完成。共 {len(official_en_to_cn)} 个英雄。")
        if new_champs:
            print(f"    🌟 发现 {len(new_champs)} 个全新英雄: "
                  f"{', '.join([official_en_to_cn[en] for en in new_champs])}")
        if renamed_champs:
            print(f"    ✏️ 发现 {len(renamed_champs)} 个改名英雄: "
                  f"{', '.join([official_en_to_cn[en] for en in renamed_champs])}")

        return official_en_to_cn, official_cn_to_en, new_champs, renamed_champs

    except Exception as e:
        print(f"!!! 官方数据同步失败，请检查网络: {e}")
        return {}, {}, [], []


# ================= 2. 拼音生成 =================
def update_pinyin_file(official_cn_to_en):
    """由英雄中文名生成拼音首字母索引，写入 pinyin_map.json。"""
    print(">>> 更新拼音检索文件...")
    from pypinyin import lazy_pinyin

    pinyin_data = {}
    for cn_name in official_cn_to_en.keys():
        pinyin_list = lazy_pinyin(cn_name)
        initials = "".join([p[0].lower() for p in pinyin_list if p])
        pinyin_data[cn_name] = initials

    with open(PINYIN_FILE, 'w', encoding='utf-8') as f:
        json.dump(pinyin_data, f, indent=4, ensure_ascii=False)
    print("    拼音文件已更新。")


# ================= 3. GitHub 兜底下载 =================
def download_from_github(log_func=None):
    """Data Dragon 连不上时，从 GitHub 仓库下载最新数据文件。"""
    _log = log_func or print

    files_to_download = [
        ("data/champions.json", CHAMPION_ID_FILE, "英雄名称映射"),
        ("data/pinyin_map.json", PINYIN_FILE, "拼音检索索引"),
    ]

    success_count = 0
    total = len(files_to_download)

    for idx, (remote_path, local_path, desc) in enumerate(files_to_download, 1):
        url = f"{GITHUB_RAW_BASE}/{remote_path}"
        _log(f"下载中 [{idx}/{total}]: {desc}...")
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                with open(local_path, 'wb') as f:
                    f.write(resp.content)
                _log(f"✅ {desc} 下载成功 ({len(resp.content)} bytes)")
                success_count += 1
            else:
                _log(f"❌ {desc} 下载失败 (HTTP {resp.status_code})")
        except Exception as e:
            _log(f"❌ {desc} 下载异常: {e}")

    _log(f"下载完成: {success_count}/{total} 成功")
    return success_count == total


# ================= 主程序 (命令行入口) =================
def main():
    print("=== 英雄数据维护工具 ===\n")
    print("请选择要执行的任务:")
    print("   [1] 同步英雄数据 + 生成拼音索引（Data Dragon 官方源）")
    print("   [2] 从 GitHub 仓库下载数据文件（官方源连不上时的兜底）")

    choice = input("\n请输入选项 (默认1): ").strip()
    if not choice:
        choice = '1'

    if choice == '2':
        download_from_github()
    else:
        official_en_to_cn, official_cn_to_en, _, _ = sync_official_data()
        if official_en_to_cn:
            update_pinyin_file(official_cn_to_en)

    print("\n✅ 任务结束。")


if __name__ == "__main__":
    main()
