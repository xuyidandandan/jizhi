#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""酷9 · 直播列表(Python)脚本 —— 江苏移动 IPTV

综合两份上游实现：
  · 江苏移动IPTV-v7.7 四件套 / updater.py  （回看地址写法、台号连续、CDN 测速缓存、自检思路）
  · justinguo7777/jsydiptv / fpk/app/server/server.py （全省视角、isLocal 地方台识别、
    多清晰度择优、4K/高清臻享归组、按内容细分小组、孤儿频道兜底）

设备路径（两者都读 py/ 目录，协议头按版本二选一）：
  py://channel/list.py       → 设备 py/channel/list.py     （PY 版，内置解释器）
  python3://channel/list.py  → 设备 py/channel/list.py     （标准版，需先装 Python3 运行时）
  酷9 → 直播列表 → 订阅地址填上面那条，解析类型不用选（原生 M3U/TXT）

可选参数（加在地址后面）：
  mode=m3u|txt        输出格式，默认 m3u（带台标/台号/回看）；txt 为「名称,地址 + 分组,#genre#」
  group=smart|orig    分组方案，默认 smart（4K/央视/卫视/江苏/地方-市/影视…）；
                      orig = 照搬运营商电视墙原分组（超高清/央视/本地/卫视/体育…）
  local=city|one      地方台分组，默认 city（地方-南通、地方-苏州…）；one = 全部并入「地方」
  chno=seq|src        台号，默认 seq（1..N 连续，方便遥控器输数字）；src = 源站台号
  back=0              不带回看（默认带，catchup + 7 天 catchup-source）
  backdays=7          回看天数，写进 catchup-days
  backfmt=index|plain 回看地址形态，默认 index（…/lookback/CODE/CODE/index.m3u8?playseek=）
  logo=0              不输出台标
  cdn=IP:PORT         指定 CDN 主机（默认自动测速并缓存最快的一台）
  scan=1              扫 223.110.247.100-255 找新 CDN（慢，找到会缓存）
  extra=1             附带 25 个轮播频道（源站「全部」里没有，来自抓包表）
  only=4K,央视        只输出这些分组（逗号分隔，可写分组名前缀）
  fresh=1             跳过频道表缓存（缓存 6 小时）
  debug=1             顶部加注释行，写明频道数/分组数/CDN/耗时

说明：脚本只产出列表文本，不转流、不代理。直播与回看地址都是运营商 CDN 直连，
      因此必须在江苏移动宽带/移动网络下使用。
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# ============================================================ 配置区

EPG_API = "http://lvpepg.uni.jsa.bcs.ottcn.com:8080/cms-lvp-epg"
UA = {"User-Agent": "okhttp/3.12.0"}

PROVINCE = "320000"          # 全省视角（必用：用某个地市视角只能拿到 200 出头个台）
HOME_CITY = "320600"         # 默认本地市（南通），只影响排序与兜底分组
ASSORT_ALL = "115"           # 全部（278 台，已含 13 市本地台，且无重名）
ASSORT_LOCAL = "120"         # 各地市「本地」
DEVICE_GROUPS = "1697"       # 实测可用；旧的 5362 已返回「无可用结果」

CITIES = [("320100", "南京"), ("320200", "无锡"), ("320300", "徐州"), ("320400", "常州"),
          ("320500", "苏州"), ("320600", "南通"), ("320700", "连云港"), ("320800", "淮安"),
          ("320900", "盐城"), ("321000", "扬州"), ("321100", "镇江"), ("321200", "泰州"),
          ("321300", "宿迁")]
CITY_NAME = dict(CITIES)
CITY_ORDER = [c for c, _n in CITIES]

# 分组顺序：4K 置顶，其后 央视 → 卫视 → 江苏 → 地方 → 按内容细分的小组
GROUP_ORDER = ("4K", "央视", "卫视", "江苏", "地方", "影视剧场", "少儿动画",
               "体育竞技", "生活纪录", "音乐广播", "购物")

# 运营商电视墙原分组（group=orig 时用）
ORIG_ORDER = ("超高清", "央视", "本地", "卫视", "体育", "专业频道", "影视",
              "少儿", "生活", "广播", "购物")
# 细分小组 → 电视墙叫法（group=orig 时给没有上游归属的孤儿频道兜底）
ORIG_MAP = {"央视": "央视", "卫视": "卫视", "江苏": "本地", "地方": "本地",
            "影视剧场": "影视", "少儿动画": "少儿", "体育竞技": "体育",
            "生活纪录": "生活", "音乐广播": "广播", "购物": "购物", "4K": "超高清"}

# 内置分组兜底表（分组接口失败时用）
ASSORT_TABLE = [("208", "超高清"), ("116", "央视"), ("120", "本地"), ("121", "卫视"),
                ("170", "体育"), ("211", "专业频道"), ("169", "影视"), ("171", "少儿"),
                ("172", "生活"), ("122", "广播"), ("173", "购物")]

# CDN 池：两套上游实测可用的主机（223.110.247.x:6610），按历史最快排序
HOSTS = ["223.110.247.216", "223.110.247.137", "223.110.247.217", "223.110.247.231",
         "223.110.247.195", "223.110.247.151", "223.110.247.134", "223.110.247.130",
         "223.110.247.200", "223.110.247.228", "223.110.247.203", "223.110.247.211"]
HOST_PORT = "6610"
SCAN_PREFIX = "223.110.247."

CACHE_NAME = ".ku9_iptv.json"
CACHE_TTL = 6 * 3600
CACHE_VER = 1
TIMEOUT = 12

# 回看占位符：${(b)/(e)} 语法 酷9 / FongMi / APTV / TVBox / DIYP 通用；
# 裸 {start} 谁都不认，Kodi 的 ${start:YmdHMS} 又是另一套，这里只发酷9能吃的。
SEEK_B = "${(b)yyyyMMddHHmmss}"
SEEK_E = "${(e)yyyyMMddHHmmss}"

# 4K / 高清臻享判定
UHD_NAME_RE = re.compile(r"4K|UHD|超高清", re.I)
ZHX_RE = re.compile(r"高清臻享")
UHD_CODE_RE = re.compile(r"(?:4K|UHD|2160)(?![0-9])", re.I)
STYLE_RE = re.compile(r"(4K|高清臻享|UHD|超高清)$", re.I)
CCTV_RE = re.compile(r"^CCTV[- ]?(\d+)", re.I)
CCTV4K_RE = re.compile(r"^CCTV-?(\d+)4K$")

LIVE_RE = re.compile(r"/(gitv|cntv|jsyd)/(?:[^/]+/)?(?:live|live1)/([^/]+)/1\.m3u8")

# 25 个轮播频道（源站「全部」清单里没有，来自抓包表；extra=1 才带）
EXTRA_TABLE = [
    ("439", "耀眼", "XKDHD-4000k-1080P-yy"), ("440", "妻子的浪漫旅行", "XKDHD-4000k-1080P-qzdlmlx26"),
    ("446", "热播短剧", "XKDHD-4000k-1080P-rbdj"), ("447", "精品短剧", "XKDHD-4000k-1080P-jpdj"),
    ("448", "爆款短剧", "XKDHD-4000k-1080P-bkdj"), ("451", "活力健身", "XKDHD-4000k-1080P-qmjsh"),
    ("610", "4K花园", "XKDHDR-50M-50p-Z4k"), ("361", "奶龙", "XKDHD-4000k-1080P-n1"),
    ("363", "东方娃娃", "XKDHD-4000k-1080P-dfww"), ("367", "斗罗大陆", "XKDHD-4000k-1080P-dldl"),
    ("384", "蓝盔特战队", "XKDHD-4000k-1080P-lktzd"), ("385", "西游记续集", "XKDHD-4000k-1080P-xyjxj"),
    ("298", "怀旧经典", "XKDHD-4000k-1080P-hjjd"), ("386", "红楼梦", "XKDHD-4000k-1080P-hlm"),
    ("398", "康熙王朝", "XKDHD-4000k-1080P-kxwc"), ("400", "养生堂", "XKDHD-4000k-1080P-yst"),
    ("421", "全民健身", "XKDHD-4000k-1080P-qmjs"), ("422", "笑满屏间", "XKDHD-4000k-1080P-xmpj"),
    ("431", "蛋仔乐园", "XKDHD-4000k-1080P-dzly"), ("432", "沙盒乐园", "XKDHD-4000k-1080P-shly"),
    ("433", "精英战场", "XKDHD-4000k-1080P-jpzc"), ("434", "动画奇遇记", "XKDHD-4000k-1080P-dhqyj"),
    ("442", "果果乐园", "XKDHD-4000k-1080P-ggly"), ("443", "游戏视界", "XKDHD-4000k-1080P-yxsj"),
    ("450", "我的世界", "XKDHD-4000k-1080P-wdsj"),
]

_LOGS = []


def log(msg):
    _LOGS.append(msg)


# ============================================================ 参数

def _s(params, key, default=""):
    v = params.get(key)
    if v is None:
        return default
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    return str(v).strip()


def _flag(params, key):
    v = _s(params, key).lower()
    return v in ("1", "true", "yes", "on", "y")


def _parse_qs(text):
    """把注入的 url / 裸查询串 / JSON 统一解成 dict。"""
    out = {}
    if not text:
        return out
    if isinstance(text, dict):
        return dict((str(k), str(v)) for k, v in text.items())
    text = str(text).strip()
    if text.startswith("{"):
        try:
            j = json.loads(text)
            if isinstance(j, dict):
                return dict((str(k), str(v)) for k, v in j.items())
        except Exception:                                   # noqa: BLE001
            return out
    if "?" in text:
        text = text.split("?", 1)[1]
    for kv in text.replace("&", ";").split(";"):
        if not kv:
            continue
        if "=" in kv:
            k, v = kv.split("=", 1)
        else:
            k, v = kv, ""
        k = urllib.parse.unquote(k.strip())
        v = urllib.parse.unquote(v.strip())
        if k:
            out[k] = v
    return out


def _gather_params():
    """兼容三种注入方式：全局 params/url（引擎注入）、argv JSON、argv 网址。"""
    g = globals()
    if isinstance(g.get("params"), dict):
        d = dict((str(k), str(v)) for k, v in g["params"].items())
        if isinstance(g.get("url"), str) and "url" not in d:
            d["url"] = g["url"]
        return d
    if len(sys.argv) > 1:
        return _parse_qs(sys.argv[1])
    return {}


# ============================================================ HTTP

def _http(url, timeout=TIMEOUT, retries=1):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:                              # noqa: BLE001
            last = e
            time.sleep(0.25 + i * 0.35)
    log("      HTTP失败 %s (%s)" % (url[:110], last))
    return b""


def _json(url, timeout=TIMEOUT):
    raw = _http(url, timeout)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8", "ignore"))
    except Exception:                                       # noqa: BLE001
        return {}


def _pmap(fn, items, workers=10):
    """并发跑 fn，任一异常都吞掉返回 None；没有线程池就串行。"""
    def safe(x):
        try:
            return fn(x)
        except Exception as e:                              # noqa: BLE001
            log("      子任务失败 %r: %s" % (x, e))
            return None
    try:
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            return list(ex.map(safe, items))
    except Exception:                                       # noqa: BLE001
        return [safe(x) for x in items]


def _ability(district=PROVINCE):
    d = {"deviceGroupIds": [DEVICE_GROUPS], "districtCode": district}
    return urllib.parse.quote(json.dumps(d, ensure_ascii=False, separators=(",", ":")), safe="")


# ============================================================ 源站接口

def _channel_list(ab, assort=ASSORT_ALL):
    url = ("%s/lvps/channelList?assortId=%s&serviceChannelId=&isShowNewProgram=true"
           "&abilityString=%s&t=%d" % (EPG_API, assort, ab, int(time.time() * 1000)))
    return (_json(url).get("content") or {}).get("channels") or []


def _uuids(ab, assort):
    return [c.get("uuid") for c in _channel_list(ab, assort) if c.get("uuid")]


def _assortments(ab):
    """运营商电视墙原分组 [(assortId, name)]，剔除 全部/收藏/最新。"""
    url = "%s/lvps/getOpenNewAssortment?serviceChannelId=&abilityString=%s" % (EPG_API, ab)
    lst = (_json(url).get("content") or {}).get("assortList") or []
    out = []
    for x in lst:
        aid, nm = str(x.get("assortId") or ""), (x.get("assortName") or "").strip()
        if aid.isdigit() and nm and nm not in ("全部", "收藏", "最新"):
            out.append((aid, nm, int(x.get("sortNum") or 99)))
    if not out:
        return [(a, n, i + 1) for i, (a, n) in enumerate(ASSORT_TABLE)]
    return [(a, n, s) for a, n, s in sorted(out, key=lambda v: v[2])]


# ============================================================ 频道解析

def _parse_url(u):
    """运营商地址里的主机被脱敏成 XXX.XXX.XXX.XXX，只取 (cp, code)，主机自己拼。"""
    m = LIVE_RE.search(u or "")
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"/live1?/([^/]+)/1\.m3u8", u or "")
    if m:
        m2 = re.search(r"/(gitv|cntv|jsyd)/", u or "")
        return (m2.group(1) if m2 else "gitv"), m.group(1)
    return None, None


def _poster(res):
    ps = (res or {}).get("poster") or []
    for t in (103, 100, None):
        for p in ps:
            if (t is None or p.get("posterType") == t) and p.get("posterUrl"):
                return p["posterUrl"]
    return ""


def _res_list(c):
    """某频道所有清晰度档，按运营商权重降序。"""
    out = []
    for r in c.get("resolution") or []:
        cp, code = _parse_url(r.get("url"))
        if not code:
            continue
        out.append({
            "name": (r.get("name") or "").strip(), "cp": cp, "code": code,
            "weight": int(r.get("weight") or 0), "logo": _poster(r),
            "is_default": (r.get("name") == c.get("defaultResolution")),
            "mc": r.get("multicastUrl") or "",
        })
    out.sort(key=lambda v: (-v["weight"], 0 if v["is_default"] else 1))
    return out


def _is_uhd_res(r):
    nm = (r.get("name") or "")
    if "高清臻享" in nm or "超高清" in nm or "4K" in nm.upper():
        return True
    return bool(UHD_CODE_RE.search(r.get("code") or ""))


def _choose(rl):
    """挑一档播：高清臻享优先，其次运营商默认档，再次权重最高的。
    为什么不直接用 defaultResolution：卫视 4K 那批默认档是「超清」(1080P)，
    真正的 4K 在「高清臻享」档里，照默认档播就变成「名字叫 4K、画面是 1080P」。"""
    if not rl:
        return None
    hit = [r for r in rl if _is_uhd_res(r)]
    if hit:
        hit.sort(key=lambda v: (0 if "高清臻享" in v["name"] else 1, -v["weight"]))
        return hit[0]
    for r in rl:
        if r["is_default"]:
            return r
    return rl[0]


def _is_4k_ch(name, code, res_name):
    """进 4K 组：名字带 4K/UHD/超高清/高清臻享，或代码带 4K 标记。
    注意 CCTV-1 不算：它名字就叫 CCTV-1，只是默认档恰好是高清臻享，
    归到 4K 组会把最常看的台藏进 4K 里；它的流照样是臻享档。"""
    if UHD_NAME_RE.search(name or "") or ZHX_RE.search(name or ""):
        return True
    return bool(UHD_CODE_RE.search(code or ""))


def _fetch_channels():
    """拉全省频道表 + 分组归属 + 地方台归属，拼成扁平清单。"""
    t0 = time.time()
    ab = _ability(PROVINCE)

    log("[1/4] 拉全省频道表 assort=%s" % ASSORT_ALL)
    raw = _channel_list(ab, ASSORT_ALL)
    if not raw:
        raise RuntimeError("频道表为空：不在江苏移动网络下，或接口已变更")
    log("      %d 个频道" % len(raw))

    log("[2/4] 拉原版分组归属")
    assorts = _assortments(ab)
    groups = [g for g in assorts if g[0] != ASSORT_ALL]
    res = _pmap(lambda g: (g[1], _uuids(ab, g[0])), groups, workers=8)
    member = {}
    for item in res:
        if not item:
            continue
        gname, uu = item
        for u in uu:
            member.setdefault(u, [])
            if gname not in member[u]:
                member[u].append(gname)
    log("      分组 %d 个，有归属 %d 个频道" % (len(groups), len(member)))

    log("[3/4] 定位 13 市地方台")
    city_lists = {}
    for item in _pmap(lambda c: (c[0], _uuids(_ability(c[0]), ASSORT_LOCAL)), CITIES, workers=8):
        if item and item[1]:
            city_lists[item[0]] = set(item[1])
    common = set()
    if len(city_lists) > 1:
        common = set.intersection(*city_lists.values())
    city_of = {}
    for code in CITY_ORDER:
        for u in city_lists.get(code) or ():
            if u not in common and u not in city_of:
                city_of[u] = code
    # 交集法全挂时退回 isLocal（上游自己打的县区台标记，实测与交集法 100% 一致）
    islocal = set(c.get("uuid") for c in raw if str(c.get("isLocal")) == "1")
    if not city_of and islocal:
        city_of = dict((u, "") for u in islocal)
        log("      交集法失败，退回 isLocal")
    else:
        for u in islocal - set(city_of):
            city_of[u] = ""
    log("      省级本地台 %d，地方台 %d（%d 市）"
        % (len(common), len(city_of), len(city_lists)))

    log("[4/4] 整理")
    chans, seen = [], set()
    for c in raw:
        uuid = c.get("uuid") or ""
        if not uuid or uuid in seen:
            continue
        rl = _res_list(c)
        chosen = _choose(rl)
        if not chosen:
            continue
        seen.add(uuid)
        num = re.sub(r"\D", "", str(c.get("num") or "")) or "0"
        chans.append({
            "uuid": uuid,
            "name": (c.get("name") or "").strip(),
            "num": int(num),
            "cp": chosen["cp"], "code": chosen["code"],
            "back": int(c.get("isShowBack") or 0),
            "logo": chosen["logo"],
            "res": chosen["name"],
            "city": city_of.get(uuid, ""),
            "local": 1 if uuid in city_of else 0,
            "otypes": member.get(uuid, []),
        })
    log("      合计 %d 个频道（耗时 %.1fs）" % (len(chans), time.time() - t0))
    return chans


# ============================================================ 分组

_CCTV_NAME_RE = re.compile(r"^CCTV[- ]?(\d+)", re.I)


def _group_by_name(n):
    if _CCTV_NAME_RE.match(n) or n.startswith("央视") or "CGTN" in n.upper() \
            or "英语新闻" in n or "CCTV" in n.upper():
        return "央视"
    if "卫视" in n:
        return "卫视"
    if "广播" in n or "之声" in n or "调频" in n or "音乐" in n:
        return "音乐广播"
    if re.search(r"财经|财金|股市|证券", n):
        return "生活纪录"
    if re.search(r"少儿|卡通|动画|佩奇|飞侠|奥特曼|熊出没|儿童|奶龙|娃娃|乐园", n):
        return "少儿动画"
    if re.search(r"体育|足球|篮球|网球|围棋|赛事|竞技|功夫|棋牌|高尔夫", n):
        return "体育竞技"
    if re.search(r"购|好物|养生|保健|食品|饮食|收藏|文旅", n):
        return "购物"
    if re.search(r"影视|剧场|电影|院线|戏曲|梨园|综艺|喜剧|短剧|院线", n):
        return "影视剧场"
    return "生活纪录"


def _classify(ch, mode="smart", local="city"):
    """定分组，返回 (组名, 排序用的基础组)。"""
    name = ch["name"]
    is4k = ch["_4k"] if "_4k" in ch else _is_4k_ch(name, ch["code"], ch.get("res") or "")

    if mode == "orig":
        # 照搬电视墙：4K 组仍单独置顶（上游「超高清」里混着 1080P 臻享，没法直接用）
        if is4k:
            return "超高清", "4K"
        for o in ch.get("otypes") or []:
            if o in ORIG_ORDER:
                return o, o
        alt = ORIG_MAP.get(_group_by_name(name), "生活")
        return alt, alt

    if is4k:
        return "4K", "4K"
    if ch.get("city"):
        return ("地方-" + CITY_NAME.get(ch["city"], "其他")) if local == "city" else "地方", "地方"
    if ch.get("_local") or ch.get("local"):
        return "地方", "地方"
    for raw, dst in (("央视", "央视"), ("卫视", "卫视"), ("本地", "江苏"),
                     ("影视", "影视剧场"), ("少儿", "少儿动画"), ("体育", "体育竞技"),
                     ("广播", "音乐广播"), ("购物", "购物")):
        if raw in (ch.get("otypes") or []):
            return dst, dst
    g = _group_by_name(name)
    return g, g


def _sort_key(ch):
    base = ch["_base"]
    rank = GROUP_ORDER.index(base) if base in GROUP_ORDER else 99
    if base == "4K":
        nm = STYLE_RE.sub("", ch["name"]).strip()
        m = CCTV_RE.match(nm)
        if m:
            return (rank, 0, 0, int(m.group(1)), ch["name"])
        if ch["name"].startswith("央视"):
            return (rank, 0, 1, ch["num"], ch["name"])
        return (rank, 0, 2, ch["num"], ch["name"])
    city = CITY_ORDER.index(ch["city"]) if (base == "地方" and ch.get("city") in CITY_ORDER) else -1
    return (rank, city, 0, ch["num"], ch["name"])


# ============================================================ CDN

def _probe_host(host, cp="gitv", code="G_CCTV-1-HQ", timeout=2.5):
    for path in ("/%s/live1/%s/%s/1.m3u8" % (cp, code, code),
                 "/gitv/live1/%s/%s/1.m3u8" % (code, code)):
        try:
            t0 = time.time()
            b = _http("http://%s%s" % (host, path), timeout, 0)
            if b[:200].find(b"#EXTM3U") >= 0:
                return host, int((time.time() - t0) * 1000)
        except Exception:                                   # noqa: BLE001
            continue
    return None


def _pick_cdn(params, cache):
    """CDN 主机：参数 > 缓存（先验一下还通不通） > 池子测速取最快 > 池子第一台。"""
    want = _s(params, "cdn")
    if want:
        return want if ":" in want else want + ":" + HOST_PORT

    def norm(h):
        return h if ":" in h else h + ":" + HOST_PORT

    scan = _flag(params, "scan")
    cached = cache.get("cdn")
    if cached and not scan:
        if _probe_host(cached, timeout=2.0):
            return cached
        log("[CDN] 缓存主机 %s 不通，重新测速" % cached)

    pool = [norm(h) for h in HOSTS]
    if scan:
        cands = ["%s%d:%s" % (SCAN_PREFIX, i, HOST_PORT) for i in range(100, 256)]
        got = [r for r in _pmap(lambda h: _probe_host(h), cands, workers=32) if r]
        if got:
            got.sort(key=lambda x: x[1])
            log("[CDN] 扫到 %d 台：%s" % (len(got), "、".join(h for h, _m in got[:6])))
            pool = [h for h, _m in got] + pool

    got = [r for r in _pmap(lambda h: _probe_host(h), pool[:6], workers=6) if r]
    if got:
        got.sort(key=lambda x: x[1])
        log("[CDN] 最快 %s（%dms）" % (got[0][0], got[0][1]))
        return got[0][0]
    log("[CDN] 池子全不通，用 %s（不在江苏移动网络下？）" % pool[0])
    return pool[0]


# ============================================================ 缓存

def _cache_path():
    here = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ""
    for d in (here, os.path.join(os.path.expanduser("~"), ".ku9_epg"),
              "/sdcard/.ku9_epg", "/tmp"):
        try:
            if d and not os.path.isdir(d):
                if d == here:
                    continue
                os.makedirs(d)
            if d and os.access(d, os.W_OK):
                return os.path.join(d, CACHE_NAME)
        except Exception:                                   # noqa: BLE001
            continue
    return ""


def _cache_read():
    p = _cache_path()
    if not p or not os.path.isfile(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            j = json.load(f)
        if j.get("ver") != CACHE_VER:
            return {}
        if time.time() - int(j.get("ts") or 0) > CACHE_TTL:
            return {"cdn": j.get("cdn")}          # 频道表过期但 CDN 可复用
        return j
    except Exception:                                       # noqa: BLE001
        return {}


def _cache_write(cache):
    p = _cache_path()
    if not p:
        return
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:                                       # noqa: BLE001
        pass


# ============================================================ 4K 实测（probe=1）
# 上游把「高清臻享」也算 4K，但实测其中一部分只是 1080P。这里按切片里的
# H.264/H.265 SPS 读真实分辨率，读不到才退回按名字判断（移植自 jsydiptv/media_probe.py）。

def _unescape(b):
    """去防竞争字节 00 00 03 -> 00 00"""
    out = bytearray()
    i, n = 0, len(b)
    while i < n:
        if i + 2 < n and b[i] == 0 and b[i + 1] == 0 and b[i + 2] == 3:
            out += b[i:i + 2]
            i += 3
        else:
            out.append(b[i])
            i += 1
    return bytes(out)


class _BR(object):
    def __init__(self, d):
        self.d, self.p = d, 0

    def bit(self):
        if self.p >> 3 >= len(self.d):
            self.p += 1
            return 0
        v = (self.d[self.p >> 3] >> (7 - (self.p & 7))) & 1
        self.p += 1
        return v

    def bits(self, n):
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def ue(self):
        z = 0
        while self.p < len(self.d) * 8:
            if self.bit():
                break
            z += 1
            if z > 32:
                return 0
        return 0 if z == 0 else (1 << z) - 1 + self.bits(z)

    def se(self):
        v = self.ue()
        return (v + 1) // 2 if v & 1 else -(v // 2)


def _sps_h264(nal):
    try:
        d = _unescape(nal[1:])
        r = _BR(d)
        profile = r.bits(8)
        r.bits(8)
        r.bits(8)                       # level
        r.ue()                          # sps_id
        chroma = 1
        if profile in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
            chroma = r.ue()
            if chroma == 3:
                r.bit()
            r.ue()
            r.ue()
            r.bit()
            if r.bit():                 # seq_scaling_matrix_present
                for i in range(12 if chroma == 3 else 8):
                    if r.bit():
                        size = 16 if i < 6 else 64
                        last = nxt = 8
                        for _ in range(size):
                            if nxt != 0:
                                nxt = (last + r.se() + 256) % 256
                            if nxt != 0:
                                last = nxt
        r.ue()                          # log2_max_frame_num_minus4
        poc = r.ue()
        if poc == 0:
            r.ue()
        elif poc == 1:
            r.bit(); r.se(); r.se()
            for _ in range(min(r.ue(), 64)):
                r.se()
        r.ue()                          # max_num_ref_frames
        r.bit()                         # gaps_in_frame_num
        w_mbs = r.ue() + 1
        h_map = r.ue() + 1
        frame_only = r.bit()
        if not frame_only:
            r.bit()
        r.bit()                         # direct_8x8_inference
        cl = cr = ct = cb = 0
        if r.bit():                     # frame_cropping
            cl, cr, ct, cb = r.ue(), r.ue(), r.ue(), r.ue()
        sub_w = 2 if chroma in (1, 2) else 1
        sub_h = 2 if chroma == 1 else 1
        w = w_mbs * 16 - (cl + cr) * sub_w
        h = (2 - frame_only) * h_map * 16 - (ct + cb) * sub_h * (2 - frame_only)
        if not (0 < w < 20000 and 0 < h < 20000):
            return None
        return {"w": w, "h": h, "codec": "H.264"}
    except Exception:                                       # noqa: BLE001
        return None


def _sps_h265(nal):
    """只取到 conformance_window，宽高之后的字段一概不解（少一半出错机会）。
    general_level_idc 是 u(8) 不是 ue，搞错会整体错位。"""
    try:
        d = _unescape(nal[2:])
        r = _BR(d)
        r.bits(4)                       # sps_video_parameter_set_id
        max_sub = r.bits(3)
        r.bits(1)
        r.bits(2)                       # profile_space
        r.bits(1)                       # tier
        profile = r.bits(5)
        r.bits(32)                      # compatibility flags
        r.bits(48)                      # constraint flags + reserved
        r.bits(8)                       # general_level_idc
        if max_sub > 0:
            sp = [r.bits(1) for _ in range(max_sub)]
            sl = [r.bits(1) for _ in range(max_sub)]
            for _ in range(max_sub, 8):
                r.bits(2)
            for i in range(max_sub):
                if sp[i]:
                    r.bits(2); r.bits(1); r.bits(5); r.bits(32); r.bits(48)
                if sl[i]:
                    r.bits(8)
        r.ue()                          # sps_seq_parameter_set_id
        chroma = r.ue()
        if chroma == 3:
            r.bit()
        w = r.ue()
        h = r.ue()
        if r.bit():                     # conformance_window
            cl, cr, ct, cb = r.ue(), r.ue(), r.ue(), r.ue()
            sub_w = 2 if chroma in (1, 2) else 1
            sub_h = 2 if chroma == 1 else 1
            w -= (cl + cr) * sub_w
            h -= (ct + cb) * sub_h
        if not (0 < w < 20000 and 0 < h < 20000):
            return None
        return {"w": w, "h": h, "codec": "H.265" if profile in (1, 2, 3) else "H.265"}
    except Exception:                                       # noqa: BLE001
        return None


def _extract_pes(ts):
    buf, sid = {}, {}
    for off in range(0, len(ts) - 187, 188):
        if ts[off] != 0x47:
            continue
        pusi = ts[off + 1] & 0x40
        pid = ((ts[off + 1] & 0x1F) << 8) | ts[off + 2]
        afc = (ts[off + 3] >> 4) & 0x03
        p = off + 4
        if afc & 0x02:
            p += 1 + ts[off + 4]
        if not (afc & 0x01) or p >= off + 188:
            continue
        seg = ts[p:off + 188]
        if pusi and len(seg) > 9 and seg[0] == 0 and seg[1] == 0 and seg[2] == 1:
            sid[pid] = seg[3]
            seg = seg[9 + seg[8]:]
        b = buf.setdefault(pid, bytearray())
        if len(b) < 400000:
            b += seg
    return dict((k, (bytes(v), sid.get(k, -1))) for k, v in buf.items())


def _find_nals(data):
    out, i, n = [], 0, len(data)
    while i < n - 4:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 1:
            start = j = i + 3
            while j < n - 3:
                if data[j] == 0 and data[j + 1] == 0 and data[j + 2] in (0, 1):
                    break
                j += 1
            out.append(data[start:j])
            i = j
        else:
            i += 1
    return out


def _probe_media(m3u8_url, nbytes=700000, timeout=10):
    """跑到切片里读 SPS，返回 {"w","h","codec"} 或 None。4K 切片很大，只读头部。"""
    body = _http(m3u8_url, timeout, 0)
    if not body:
        return None
    try:
        text = body.decode("utf-8", "replace")
    except Exception:                                       # noqa: BLE001
        return None
    segs = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not segs:
        return None
    seg = segs[-2] if len(segs) >= 2 else segs[-1]      # 最后一片可能还在写
    if not seg.startswith("http"):
        seg = m3u8_url.rsplit("/", 1)[0] + "/" + seg
    try:
        req = urllib.request.Request(seg, headers=dict(UA, Range="bytes=0-%d" % (nbytes - 1)))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ts = r.read(nbytes)
    except Exception:                                       # noqa: BLE001
        return None
    if not ts or ts[0] != 0x47:
        return None
    for _pid, (b, s) in _extract_pes(ts).items():
        if not (0xE0 <= s <= 0xEF):
            continue
        for nal in _find_nals(b):
            if not nal:
                continue
            if (nal[0] & 0x1F) == 7:
                info = _sps_h264(nal)
            elif len(nal) >= 2 and ((nal[0] >> 1) & 0x3F) == 33:
                info = _sps_h265(nal)
            else:
                continue
            if info:
                return info
    return None


def _probe_4k(chans, cdn):
    """给 4K 组候选打真实分辨率标记：c["w"]/c["h"]/c["codec"]，读不到就留空。"""
    todo = [c for c in chans if c.get("_4k") and not c.get("w")]
    if not todo:
        return
    log("[probe] 实测 %d 个候选分辨率" % len(todo))

    def one(c):
        for i in range(2):
            info = _probe_media("http://%s/%s/live1/%s/%s/1.m3u8"
                                % (cdn, c["cp"], c["code"], c["code"]))
            if info and info.get("w"):
                return info
            time.sleep(0.3 + i * 0.5)
        return None

    for c, info in zip(todo, _pmap(one, todo, workers=4)):
        if info:
            c["w"], c["h"], c["codec"] = info["w"], info["h"], info.get("codec") or ""
    ok = sum(1 for c in todo if c.get("w"))
    n4k = sum(1 for c in todo if (c.get("w") or 0) >= 3840 or (c.get("h") or 0) >= 2160)
    log("      实测成功 %d/%d，其中真 4K %d 个" % (ok, len(todo), n4k))


# ============================================================ 输出

def _attr(v):
    return str(v or "").replace('"', "'").replace("\r", " ").replace("\n", " ")


def _live_url(cdn, ch):
    return "http://%s/%s/live1/%s/%s/1.m3u8" % (cdn, ch["cp"], ch["code"], ch["code"])


def _back_url(cdn, ch, fmt="index"):
    """回看地址：真实路径是 /{cp}/lookback/CODE/CODE(+ /index.m3u8)，与直播 /live1/ 不同段，
    所以 catchup 必须用 default（整条当回看地址），不能用 append（往直播地址后面拼参数）。"""
    tail = "/index.m3u8" if fmt != "plain" else ""
    return ("http://%s/%s/lookback/%s/%s%s?playseek=%s-%s"
            % (cdn, ch["cp"], ch["code"], ch["code"], tail, SEEK_B, SEEK_E))


def _render_m3u(chans, cdn, opt):
    lines = ["#EXTM3U"]
    if opt["debug"]:
        lines.append("# 江苏移动IPTV｜%d 频道｜分组 %d｜CDN %s｜%s"
                     % (len(chans), len(set(c["_label"] for c in chans)), cdn,
                        time.strftime("%Y-%m-%d %H:%M")))
    no = 0
    for ch in chans:
        no += 1
        chno = no if opt["chno"] == "seq" else ch["num"]
        attrs = ['tvg-id="%s"' % _attr(ch["uuid"]),
                 'tvg-name="%s"' % _attr(ch["name"]),
                 'tvg-chno="%s"' % chno]
        if opt["logo"] and ch.get("logo"):
            attrs.append('tvg-logo="%s"' % _attr(ch["logo"]))
        attrs.append('group-title="%s"' % _attr(ch["_label"]))
        if opt["back"] and ch.get("back") > 0:
            attrs.append('catchup="default"')
            attrs.append('catchup-days="%d"' % opt["backdays"])
            attrs.append('catchup-source="%s"' % _attr(_back_url(cdn, ch, opt["backfmt"])))
        lines.append("#EXTINF:-1 %s,%s" % (" ".join(attrs), _attr(ch["name"])))
        lines.append(_live_url(cdn, ch))
    return "\n".join(lines) + "\n"


def _render_txt(chans, cdn, opt):
    out, cur = [], None
    for ch in chans:
        if ch["_label"] != cur:
            cur = ch["_label"]
            out.append("%s,#genre#" % cur)
        out.append("%s,%s" % (ch["name"], _live_url(cdn, ch)))
    return "\n".join(out) + "\n"


def _render(chans, cdn, opt):
    if opt["mode"] == "txt":
        return _render_txt(chans, cdn, opt)
    return _render_m3u(chans, cdn, opt)


# ============================================================ 主流程

def build(params=None):
    """返回列表正文（字符串）。"""
    del _LOGS[:]
    params = params if isinstance(params, dict) else _gather_params()
    opt = {
        "mode": (_s(params, "mode") or "m3u").lower(),
        "group": (_s(params, "group") or "smart").lower(),
        "local": (_s(params, "local") or "city").lower(),
        "chno": (_s(params, "chno") or "seq").lower(),
        "backfmt": (_s(params, "backfmt") or "index").lower(),
        "back": not (_s(params, "back") in ("0", "false", "no")),
        "logo": not (_s(params, "logo") in ("0", "false", "no")),
        "debug": _flag(params, "debug"),
        "backdays": 7,
    }
    try:
        opt["backdays"] = max(1, int(_s(params, "backdays") or 7))
    except Exception:                                       # noqa: BLE001
        pass

    cache = _cache_read()
    fresh = _flag(params, "fresh")
    chans = None if fresh else cache.get("channels")
    if chans:
        log("[0/4] 命中频道缓存（%d 个，%.0f 分钟前）"
            % (len(chans), (time.time() - int(cache.get("ts") or 0)) / 60))
    else:
        chans = _fetch_channels()

    if _flag(params, "extra"):
        have = set(c["code"] for c in chans)
        for num, name, code in EXTRA_TABLE:
            if code in have:
                continue
            chans.append({"uuid": code, "name": name, "num": int(num), "cp": "gitv",
                          "code": code, "back": 1, "logo": "", "res": "轮播",
                          "city": "", "otypes": [], "_extra": True})

    for ch in chans:
        ch["_4k"] = _is_4k_ch(ch["name"], ch["code"], ch.get("res") or "")
        ch["_local"] = bool(ch.get("local") or ch.get("city"))

    cdn = _pick_cdn(params, cache)

    if _flag(params, "probe"):
        _probe_4k(chans, cdn)
    # 实测过的按实测归组（缓存里带 w/h，下次不用重测也照样生效）
    for ch in chans:
        if ch["_4k"] and ch.get("w") and ch["w"] < 3840 and (ch.get("h") or 0) < 2160:
            ch["_4k"] = False            # 实测不是 4K，退回它本来的分组（不误导）

    for ch in chans:
        label, base = _classify(ch, opt["group"], opt["local"])
        if ch.get("_extra"):
            label, base = "轮播点播", "生活纪录"
        if base == "4K":
            m = CCTV4K_RE.match(ch["name"])
            if m:
                ch["name"] = "CCTV-%s 4K" % m.group(1)
        ch["_label"], ch["_base"] = label, base

    # 4K 组内同流去重（同一路流只留台号最小的一条）
    seen = {}
    keep = []
    for ch in sorted([c for c in chans if c["_base"] == "4K"], key=_sort_key):
        k = (ch["cp"], ch["code"])
        if k in seen:
            continue
        seen[k] = ch
        keep.append(ch)
    if seen:
        chans = [c for c in chans if c["_base"] != "4K"] + keep

    chans.sort(key=_sort_key)

    # 写缓存用完整清单：只写渲染用的那一份的话，带 only= 请求一次就把缓存截断了
    full = chans
    only = _s(params, "only")
    if only:
        keys = [k.strip() for k in only.replace("，", ",").split(",") if k.strip()]
        chans = [c for c in full if any(c["_label"].startswith(k) for k in keys)]

    text = _render(chans, cdn, opt)

    if opt["debug"]:
        log("[out] %d 频道 / %d 分组 / %d 字节"
            % (len(chans), len(set(c["_label"] for c in chans)), len(text)))
        for line in _LOGS:
            sys.stderr.write(line + "\n")

    # 缓存只存原始字段（分组在渲染时重算，这样 group=/local= 换方案不用重新拉接口）
    try:
        plain = [dict((k, v) for k, v in ch.items() if not k.startswith("_"))
                 for ch in full]
        _cache_write({"ver": CACHE_VER, "ts": int(time.time()), "cdn": cdn,
                      "channels": plain})
    except Exception:                                       # noqa: BLE001
        pass
    return text


def main(params=None):
    """python3:// 入口：返回 {"data": 正文}。"""
    return {"data": build(params)}


try:
    from base.parser import Parser as _BaseParser          # PY 版内置解释器
except Exception:                                           # noqa: BLE001
    _BaseParser = object


class Parser(_BaseParser):
    """py:// 入口（PY 版必须继承 base.parser.Parser）。"""

    def parse(self, params):
        return {"data": build(params if isinstance(params, dict) else _gather_params())}

    def stop(self):
        pass


if __name__ == "__main__":
    _p = _gather_params()
    try:
        sys.stdout.write(build(_p))
        sys.stdout.flush()
    except BrokenPipeError:                                 # 管道被 head 之类提前关掉
        pass
