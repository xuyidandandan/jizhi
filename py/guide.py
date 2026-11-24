# -*- coding: utf-8 -*-
"""
酷9 EPG 脚本   →  DIYP 格式

数据来源：
  1) 频道表  /lvps/channelList           -> {uuid, name}
  2) 节目单  /lvps/getAllProgramlist     -> programs[{programName, startTime, endTime}]

酷9 订阅配置：
  地址：      py://epg/guide.py        （PY 版内置解释器，文件放设备 py/epg/guide.py）
  解析类型：  DIYP

可选参数（写在地址后，如 py://epg/guide.py?icon=0&timetype=full）：
  ch=频道名        一般由 App 自动注入，可手动覆盖
  date=日期        一般由 App 自动注入，支持 20261123 / 2026-11-23 / 2026/11/23
  uuid=频道uuid    直接指定源站 uuid，跳过频道名匹配
  icon=0|1         是否在 EPG 里带台标（默认 1，取源站 posterType=103）
  timetype=hhmm|full  时间格式，默认 hhmm（08:00）；full 为 2026-11-23 08:00:00
  offset=分钟      整体时间偏移，默认 0
  nocache=1        不使用频道表本地缓存（默认缓存 6 小时）

也可用于 python3://（外部 Python3）：脚本同时提供 def main(params)。
"""

import os
import re
import json
import time
import datetime

try:
    from base.parser import Parser as _Ku9ParserBase   # py:// (PY 版内置 Chaquopy)
except Exception:
    class _Ku9ParserBase(object):                       # python3:// 兜底
        def __init__(self, *a, **kw):
            self.address = ""

try:
    import urllib.request as _urlreq
    import urllib.parse as _urlparse
except Exception:
    _urlreq = None
    _urlparse = None


# ----------------------------------------------------------------------------
# 配置区
# ----------------------------------------------------------------------------
BASE = "http://lvpepg.uni.jsa.bcs.ottcn.com:8080"
ABILITY = '{"deviceGroupIds":["5362"],"districtCode":"320000"}'
HEADERS = {
    "User-Agent": "okhttp/3.12.11",
    "Accept": "application/json, text/plain, */*",
}
TIMEOUT = 8
CACHE_TTL = 6 * 3600          # 频道表缓存秒数
TZ = datetime.timezone(datetime.timedelta(hours=8))

# 手动别名：播放列表里的台名 -> 源站台名（按需增删）
NAME_ALIAS = {
    "南通新闻综合": "南通一套",     # 南通台新闻综合频道
    "南通新闻频道": "南通一套",
    "南通公共": "公共崇川",
    # "南通电视台一套": "南通一套",
}

# 归一化时要丢掉的后缀/干扰词（小写比较，从长到短反复剥离）
NOISE_WORDS = [
    "高清臻享", "臻享", "超高清", "高清", "超清", "标清", "蓝光", "4k", "uhd", "fhd", "hd", "sd",
    "频道", "电视", "直播",
]
NOISE_CHARS = " \t\u3000-_·•.、,，()（）[]【】<>《》'\"＋|/"

# 中文数字 -> 阿拉伯数字（只在 套/台 前替换，避免误伤地名）
CN_NUM = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5",
          "六": "6", "七": "7", "八": "8", "九": "9"}


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def _ability_qs():
    return _urlparse.quote(ABILITY, safe="")


def _http_get_json(url, retry=1):
    """GET 并解析 JSON，返回 dict；失败返回 None。"""
    if _urlreq is None:
        return None
    for _ in range(retry + 1):
        try:
            req = _urlreq.Request(url, headers=HEADERS)
            with _urlreq.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read()
            return json.loads(raw.decode("utf-8", "ignore"))
        except Exception:           # noqa: BLE001
            time.sleep(0.3)
    return None


def _norm(name):
    """台名归一化：去空格标点、去高清/频道等干扰词、中文数字转阿拉伯，便于模糊匹配。"""
    if not name:
        return ""
    s = str(name).strip().lower()
    # 加号统一：全角＋ / plus 都当作 CCTV5+ 的加号，绝不能当噪声删掉
    s = s.replace("＋", "+").replace("plus", "+")
    for ch in NOISE_CHARS:
        s = s.replace(ch, "")
    # 「一套 / 二台」->「1套 / 2台」
    for k, v in CN_NUM.items():
        s = s.replace(k + "套", v + "套").replace(k + "台", v + "台")
    s = s.replace("i套", "1套")
    changed = True
    while changed:
        changed = False
        for w in NOISE_WORDS:
            if s.endswith(w) and len(s) > len(w):
                s = s[: -len(w)]
                changed = True
    s = s.replace("中央", "cctv")
    return s


def difflib_ratio(a, b):
    """纯相似度（不做加权），用于同名候选之间挑最贴近的。"""
    import difflib
    return difflib.SequenceMatcher(None, a or "", b or "").ratio()


def _similarity(a, b):
    """归一化台名相似度打分：同前缀、互相包含都加权，避免跨城市误配。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 2.0
    import difflib
    score = difflib.SequenceMatcher(None, a, b).ratio()
    if a[:2] == b[:2]:
        score += 0.60          # 同城市/同前缀强加权（南通X vs 南京X 不能互串）
    if a in b or b in a:
        score += 0.30
    return score


def _today():
    return datetime.datetime.now(TZ).date()


def _parse_date(raw):
    """把各种日期写法归一成 date 对象：20261123 / 2026-11-23 / 11-23 / 2026/11/23 12:00:00"""
    s = (raw or "").strip()
    if not s:
        return _today()
    digits = re.sub(r"\D", "", s)
    try:
        if len(digits) >= 8:
            return datetime.date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
        if len(digits) == 4:                        # 11-23 这种缺年份的，按今年补
            return datetime.date(_today().year, int(digits[0:2]), int(digits[2:4]))
        if digits.isdigit() and int(digits) <= 6:   # 兼容 date=1 表示明天
            return _today() + datetime.timedelta(days=int(digits))
    except Exception:                               # noqa: BLE001
        pass
    return _today()


def _fmt_ts(ts, timetype):
    dt = datetime.datetime.fromtimestamp(int(ts), TZ)
    if timetype == "full":
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return dt.strftime("%H:%M")


# ----------------------------------------------------------------------------
# 频道表（带本地缓存）
# ----------------------------------------------------------------------------
def _cache_path():
    """选一个可写目录放频道表缓存。"""
    cands = []
    try:
        if "__file__" in globals():
            cands.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:                                   # noqa: BLE001
        pass
    try:
        home = os.path.expanduser("~")
        if home and home != "~":
            cands.append(os.path.join(home, ".ku9_epg"))
    except Exception:                                   # noqa: BLE001
        pass
    cands += ["/sdcard/.ku9_epg", "/tmp"]

    for d in cands:
        try:
            if not d:
                continue
            if not os.path.isdir(d):
                if not d.endswith(".ku9_epg"):          # 只主动新建自己的缓存目录
                    continue
                os.makedirs(d, exist_ok=True)
            if os.access(d, os.W_OK):
                return os.path.join(d, ".ku9_channels.json")
        except Exception:                               # noqa: BLE001
            continue
    return ""


def _load_channels(use_cache=True):
    """返回 [{uuid,name,logo}]；优先本地缓存，接口失败时退回旧缓存。"""
    path = _cache_path() if use_cache else ""
    cached = None
    if path and os.path.isfile(path):
        try:
            if time.time() - os.path.getmtime(path) < CACHE_TTL:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data:
                    return data
            else:
                with open(path, "r", encoding="utf-8") as f:
                    cached = json.load(f) or None
        except Exception:                               # noqa: BLE001
            cached = None

    url = "%s/cms-lvp-epg/lvps/channelList?abilityString=%s" % (BASE, _ability_qs())
    js = _http_get_json(url)
    out = []
    if js:
        for c in ((js.get("content") or {}).get("channels") or []):
            uuid = c.get("uuid") or ""
            name = c.get("name") or ""
            if not uuid:
                continue
            logo = ""
            for r in (c.get("resolution") or []):
                for p in (r.get("poster") or []):
                    if p.get("posterType") == 103 and p.get("posterUrl"):
                        logo = p["posterUrl"]
                        break
                if logo:
                    break
            out.append({"uuid": uuid, "name": name, "logo": logo})

    if out and path:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
            os.replace(tmp, path)                       # 原子替换，避免并发写坏
        except Exception:                               # noqa: BLE001
            pass
    return out or cached or []


def _has_plus(name):
    """是否「+」频道（CCTV5+ / CCTV5plus / CCTV5＋）。"""
    s = str(name or "").lower()
    return ("+" in s) or ("＋" in s) or ("plus" in s)


def _resolve_uuid(ch, channels, uuid_hint=""):
    """台名 -> uuid。返回 (uuid, 匹配到的台名, logo)"""
    if uuid_hint:
        for c in channels:
            if c["uuid"] == uuid_hint:
                return c["uuid"], c["name"], c["logo"]
        return uuid_hint, ch, ""

    if not ch:
        return "", "", ""

    # 1) 本身就是 uuid
    for c in channels:
        if c["uuid"] == ch:
            return c["uuid"], c["name"], c["logo"]

    # 2) 手动别名
    target = NAME_ALIAS.get(ch) or NAME_ALIAS.get(str(ch).strip())
    if target:
        for c in channels:
            if c["name"] == target:
                return c["uuid"], c["name"], c["logo"]

    # 2.5) 加号频道硬隔离：CCTV5 / CCTV-5 与 CCTV5+ 是两个不同的台，
    #      带不带 + 必须对上，否则宁可空着也不错配
    pool = channels
    if any(_has_plus(c["name"]) for c in channels):
        narrowed = [c for c in channels if _has_plus(c["name"]) == _has_plus(ch)]
        if narrowed or _has_plus(ch):
            pool = narrowed

    # 3) 完全一致
    for c in pool:
        if c["name"] == ch:
            return c["uuid"], c["name"], c["logo"]

    # 4) 归一化后完全一致（CCTV-5 / CCTV5 归一后同为 cctv5，取更贴近原名、更短的）
    n = _norm(ch)
    if n:
        hits = [c for c in pool if _norm(c["name"]) == n]
        if len(hits) == 1:
            c = hits[0]
            return c["uuid"], c["name"], c["logo"]
        if hits:
            # 归一化撞车时（如 CCTV-5 vs CCTV5高清臻享），优先「原名最干净」的那条，
            # 也就是去掉高清/臻享等修饰后没剩多少多余字的主频道
            c = max(hits, key=lambda x: (
                -(len(x["name"]) - len(_norm(x["name"]))),
                difflib_ratio(ch, x["name"]),
            ))
            return c["uuid"], c["name"], c["logo"]

    # 5) 相似度打分兜底（同前缀加权，跨城市不会被抢走）
    #    阈值 0.9：必须「同前缀 + 高相似」或「互相包含」才算命中，
    #    否则宁可返回空节目单，也不给频道配错台（如 苏州一套 ≠ 泰州一套）
    best_score, best = 0.0, None
    for c in pool:
        s = _similarity(n, _norm(c["name"]))
        if s > best_score:
            best_score, best = s, c
    if best is not None and best_score >= 0.9:
        return best["uuid"], best["name"], best["logo"]

    return "", "", ""


# ----------------------------------------------------------------------------
# 节目单
# ----------------------------------------------------------------------------
def _fetch_programs(uuid, day):
    ds = day.strftime("%Y%m%d")
    url = ("%s/cms-lvp-epg/lvps/getAllProgramlist?abilityString=%s"
           "&startDate=%s&endDate=%s&uuid=%s") % (BASE, _ability_qs(), ds, ds, uuid)
    js = _http_get_json(url, retry=2)
    if not js:
        return []
    content = js.get("content") or []
    if isinstance(content, dict):
        content = [content]

    items = []
    for g in content:
        for p in (g.get("programs") or []):
            try:
                st, et = int(p.get("startTime") or 0), int(p.get("endTime") or 0)
            except Exception:                           # noqa: BLE001
                continue
            if not st or not et or et <= st:
                continue
            items.append({
                "t": p.get("programName") or "",
                "st": st,
                "et": et,
                "desc": p.get("tvsouName") or "",
            })

    # 去重 + 按开始时间升序
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["st"]):
        key = (it["st"], it["et"], it["t"])
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _build_diyp(ch, day, matched_name, logo, programs, timetype="hhmm", offset=0, with_icon=True):
    epg_data = []
    for p in programs:
        epg_data.append({
            "start": _fmt_ts(p["st"] + offset * 60, timetype),
            "end": _fmt_ts(p["et"] + offset * 60, timetype),
            "title": p["t"],
            "desc": p["desc"],
        })
    data = {
        "channel_name": ch or matched_name,
        "date": day.strftime("%Y-%m-%d"),
        "epg_data": epg_data,
    }
    if with_icon and logo:
        data["icon"] = logo
        data["logo"] = logo
    return data


def build_epg(params):
    """核心逻辑：参数 -> DIYP 字符串"""
    params = params or {}
    raw_ch = params.get("ch") or params.get("channel") or params.get("name") or ""
    day = _parse_date(params.get("date") or params.get("startDate") or "")
    uuid_hint = (params.get("uuid") or "").strip()

    timetype = (params.get("timetype") or "hhmm").lower()
    try:
        offset = int(params.get("offset") or 0)
    except Exception:                                   # noqa: BLE001
        offset = 0
    with_icon = str(params.get("icon", "1")) not in ("0", "false", "no")
    use_cache = str(params.get("nocache", "0")) not in ("1", "true", "yes")

    channels = _load_channels(use_cache=use_cache)
    uuid, matched_name, logo = _resolve_uuid(raw_ch, channels, uuid_hint)

    programs = []
    if uuid:
        programs = _fetch_programs(uuid, day)

    data = _build_diyp(raw_ch or matched_name, day, matched_name, logo,
                       programs, timetype, offset, with_icon)
    if str(params.get("debug", "0")) in ("1", "true", "yes"):
        data["_matched"] = matched_name
        data["_uuid"] = uuid
        data["_channels"] = len(channels)
    return json.dumps(data, ensure_ascii=False)


# ----------------------------------------------------------------------------
# 酷9 入口
# ----------------------------------------------------------------------------
class Parser(_Ku9ParserBase):
    """py:// 入口：class Parser + parse"""

    def parse(self, params):
        try:
            return {"data": build_epg(params)}
        except Exception as e:                          # noqa: BLE001
            return {"data": json.dumps({
                "channel_name": (params or {}).get("ch", ""),
                "date": _parse_date((params or {}).get("date", "")).strftime("%Y-%m-%d"),
                "epg_data": [],
                "err": str(e),
            }, ensure_ascii=False)}

    def stop(self):
        pass


def main(params=None):
    """python3:// 入口"""
    try:
        return {"data": build_epg(params or {})}
    except Exception as e:                              # noqa: BLE001
        return {"data": json.dumps({"channel_name": "", "epg_data": [], "err": str(e)},
                                   ensure_ascii=False)}


# ----------------------------------------------------------------------------
# 本机调试：python guide.py 南通一套 2026-11-23
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    ch = sys.argv[1] if len(sys.argv) > 1 else "南通一套"
    dt_ = sys.argv[2] if len(sys.argv) > 2 else ""
    print(build_epg({"ch": ch, "date": dt_}))
