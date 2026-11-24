#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
江苏移动 IPTV/OTT 直播源 · 回看 · 节目单 一键归档与自动更新（单文件，纯标准库）

一个文件做完所有事：
  拉 13 个地市频道 -> 拉节目单 -> 整理分组 -> 生成 11 个文件 -> 自检 -> 发布 -> 开网页服务 -> 每天定时重跑

用法：
    python3 updater.py                 # 全江苏(278 台) + 开服务 + 每天 03:30 自动更新
    python3 updater.py --once          # 只生成一次
    python3 updater.py --district 320600          # 只要南通
    python3 updater.py --district 320100,320500   # 南京+苏州

参数：
    --out DIR        输出目录（默认脚本旁 data）
    --port 12123     网页端口（默认，避开常用端口）
    --days 3         节目单天数
    --backup-days 7  回看天数（写进 m3u catchup-days）
    --district       地市码 / 逗号多个 / all（默认 all）

安全设计：
    生成到 .build 临时目录 -> 抽样实测直播与回看 -> 通过才原子替换到正式目录
    任何一个 CDN 不通会换下一个；全部不通则保留上一版，直播永远有东西可播
"""

import argparse
import concurrent.futures
import csv
import functools
import hashlib
import http.server
import json
import os
import random
import re
import shutil
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
EPG_BASE = "http://lvpepg.uni.jsa.bcs.ottcn.com:8080/cms-lvp-epg"
ASSORT_ID = "115"
TZ8 = timezone(timedelta(hours=8))
UA = {"User-Agent": "okhttp/3.12.0"}

# CDN 候选（江苏移动，全部来自抓包实测；同 IP 的 6410 端口也能出流）
# 顺序：上次成功的 -> 下列顺序。全部不通时用 --discover 扫段重新找
HOSTS = [
    "223.110.247.231:6610",
    "223.110.247.216:6610",
    "223.110.247.195:6610",
    "223.110.247.151:6610",
    "223.110.247.137:6610",
    "223.110.247.134:6610",
    "223.110.247.130:6610",
    "223.110.247.200:6610",
    "223.110.247.228:6610",
    "223.110.247.203:6610",
    "223.110.247.211:6610",
    "223.110.247.217:6610",
]
SCAN_RANGE = ("223.110.247.", 100, 255)

# 江苏 13 个地市。districtCode 只决定多出哪几个本地台，其余频道全省相同
JIANGSU = [
    ("320100", "南京"), ("320200", "无锡"), ("320300", "徐州"), ("320400", "常州"),
    ("320500", "苏州"), ("320600", "南通"), ("320700", "连云港"), ("320800", "淮安"),
    ("320900", "盐城"), ("321000", "扬州"), ("321100", "镇江"), ("321200", "泰州"),
    ("321300", "宿迁"),
]
DNAME = dict(JIANGSU)
GROUP_ORDER = ("央视", "卫视", "江苏", "地方", "其他")

# 只有这两个字段是必须的（实测 13 个地市全量一致）。
# 经纬度、村码、账号、令牌全部不需要 —— 既是隐私，也是稳定性。
ABILITY_BASE = {"deviceGroupIds": ["1697"]}

# 回看变量三种写法（路径必须是 /lookback/，追加到 live1 上会被服务端忽略）
STYLES = (
    ("diyp",   "直播源.m3u",              "{start}",              "{end}"),
    ("kodi",   "直播源_kodi.m3u",         "${start}",             "${end}"),
    ("fongmi", "直播源_tvbox_fongmi.m3u",  "${(b)yyyyMMddHHmmss}", "${(e)yyyyMMddHHmmss}"),
)

RUN_AT = (3, 30)
PRIVACY_PATTERNS = [
    (r"1[3-9]\d{9}", "手机号"),
    (r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", "MAC 地址"),
    (r"00411300\d{10}", "STBID"),
    (r"OTTUserToken|IASHttpSessionId|accountinfo|UserName", "账号令牌字段"),
]


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def http_get(url, timeout=25, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(0.5)
    raise last


def http_get_bytes(url, timeout=8, retries=1):
    """取二进制（台标）。失败抛异常。"""
    last = None
    for _ in range(retries + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(0.2)
    raise last


LOGO_DIR = os.path.join(HERE, "logo")     # 台标缓存目录（与 www 分开，www 仍只放 3 个文件）


def localize_logos(chs, base_url):
    """把外网台标抓回本地，m3u/EPG 里的地址改成 http://NAS:端口/logo/xxx.png
    好处：不受外网台标库限速/波动影响（TVBox 里「台标一开始有后来没有」就是外网掉图）
    失败的单条保持原外网地址，不影响直播。返回本地化成功的条数。"""
    if not base_url:
        return 0
    base = base_url.rstrip("/")
    os.makedirs(LOGO_DIR, exist_ok=True)

    def one(ch):
        u = ch.get("logo") or ""
        if not u:
            return False
        fn = hashlib.md5(u.encode("utf-8")).hexdigest()[:12] + ".png"
        p = os.path.join(LOGO_DIR, fn)
        if not (os.path.exists(p) and os.path.getsize(p) > 200):
            try:
                data = http_get_bytes(u)
                if len(data) < 200:
                    return False
                with open(p, "wb") as f:
                    f.write(data)
            except Exception:       # noqa: BLE001
                return False
        ch["logo"] = base + "/logo/" + fn
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        got = sum(1 for r in ex.map(one, chs) if r)
    return got


def _run_at():
    v = os.environ.get("UPDATE_AT", "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", v)
    if m and 0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60:
        return (int(m.group(1)), int(m.group(2)))
    return RUN_AT


def load_ability(district="320600"):
    """只保留设备分组号 + 地市码，其它一概不带（隐私 + 稳定）"""
    d = dict(ABILITY_BASE)
    d["districtCode"] = district
    return urllib.parse.quote(json.dumps(d, ensure_ascii=False, separators=(",", ":")), safe="")


def ordered_hosts(cache_file):
    """顺序：缓存 -> HOSTS -> hosts.txt（用户自己加的）-> 同 IP 的 6410 端口"""
    def read_lines(p):
        out = []
        if os.path.exists(p):
            try:
                for l in open(p, encoding="utf-8").read().splitlines():
                    l = l.split("#")[0].strip()
                    if l:
                        out.append(l)
            except Exception:  # noqa: BLE001
                pass
        return out

    hs = []
    if os.path.exists(cache_file):
        c = read_lines(cache_file)[:1]
        hs += c
    hs += HOSTS
    hs += read_lines(os.path.join(HERE, "hosts.txt"))
    hs += [h.replace(":6610", ":6410") for h in hs if h.endswith(":6610")]
    out = []
    for h in hs:
        if h not in out:
            out.append(h)
    return out


def discover(limit=None):
    """扫描 223.110.247.100-255 的 6610/6410，找还能出流的 CDN（IP 全部失效时用）"""
    pre, lo, hi = SCAN_RANGE
    cands = ["%s%d:%d" % (pre, i, p) for i in range(lo, hi + 1) for p in (6610, 6410)]
    if limit:
        cands = cands[:limit]
    log("扫描 %d 个候选地址 ..." % len(cands))

    def probe(h):
        try:
            b = http_get("http://%s/gitv/live1/G_CCTV-1-HQ/G_CCTV-1-HQ/1.m3u8" % h, timeout=3, retries=0)
            return h if "#EXTM3U" in b else None
        except Exception:  # noqa: BLE001
            return None

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
        for r in ex.map(probe, cands):
            if r:
                found.append(r)
    log("发现可用 CDN %d 个: %s" % (len(found), found[:8]))
    if found:
        p = os.path.join(HERE, "hosts.txt")
        old = set(l.split("#")[0].strip() for l in (open(p, encoding="utf-8").readlines() if os.path.exists(p) else []))
        with open(p, "a", encoding="utf-8") as f:
            f.write("# --discover %s 扫到\n" % time.strftime("%Y-%m-%d %H:%M"))
            for h in found:
                if h not in old:
                    f.write(h + "\n")
    return found


def build_channels(ability, district="", quiet=False):
    if not quiet:
        log("[1/5] 拉频道表 (%s %s) ..." % (district, DNAME.get(district, "")))
    url = "%s/lvps/channelList?assortId=%s&serviceChannelId=&abilityString=%s" % (EPG_BASE, ASSORT_ID, ability)
    j = json.loads(http_get(url))
    if j.get("resultCode") != "000":
        raise RuntimeError("channelList 失败: %s" % j.get("resultMessage"))
    out = []
    for c in j["content"]["channels"]:
        res = None
        for r in c.get("resolution") or []:
            if r.get("name") == c.get("defaultResolution"):
                res = r
                break
        if res is None and c.get("resolution"):
            res = c["resolution"][0]
        if not res:
            continue
        m = re.search(r"/live/(.*?)/1\.m3u8", res.get("url") or "")
        if not m:
            continue
        code, cp = m.group(1), "gitv"
        m2 = re.search(r"/(gitv|cntv|jsyd)/", res.get("url") or "")
        if m2:
            cp = m2.group(1)
        logo = ""
        for p in res.get("poster") or []:
            if p.get("posterType") == 103:
                logo = p.get("posterUrl") or ""
                break
        if not logo and res.get("poster"):
            logo = res["poster"][0].get("posterUrl") or ""
        name = c.get("name") or ""
        num = str(c.get("num") or "")
        if name.startswith("CCTV") or "央视" in name:
            group = "央视"
        elif "卫视" in name:
            group = "卫视"
        elif num.startswith("9"):
            group = "地方"
        elif "江苏" in name or code.upper().startswith(("G_JS", "JS")):
            group = "江苏"
        else:
            group = "其他"
        out.append({"num": num, "name": name, "uuid": c.get("uuid"), "code": code, "cp": cp,
                    "back": int(c.get("isShowBack") or 0), "mc": res.get("multicastUrl") or "",
                    "logo": logo, "group": group, "district": district})
    if not quiet:
        log("      频道 %d" % len(out))
    return out


def build_programs(channels, ability, hist_days, days, quiet=False):
    if not quiet:
        log("[2/5] 拉节目单（过去 %d 天 ~ 未来 %d 天）..." % (hist_days, days))
    today = datetime.now(TZ8)
    base = ("%s/lvps/getAllProgramlist?startDate=%s&endDate=%s&pos=fullplayer&allDay=true"
            "&channelType=&relatedId=&copyrightTip=&serviceChannelId=&abilityString=%s"
            % (EPG_BASE, (today - timedelta(days=hist_days)).strftime("%Y%m%d"),
               (today + timedelta(days=days)).strftime("%Y%m%d"), ability))
    progs, done = [], [0]

    def one(ch):
        try:
            j = json.loads(http_get(base + "&uuid=" + str(ch["uuid"])))
        except Exception:  # noqa: BLE001
            return []
        r = []
        for d in j.get("content") or []:
            for p in d.get("programs") or []:
                r.append({"uuid": ch["uuid"], "title": p.get("programName") or "",
                          "start": int(p.get("startTime") or 0), "end": int(p.get("endTime") or 0)})
        return r

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for f in concurrent.futures.as_completed([ex.submit(one, c) for c in channels]):
            progs.extend(f.result() or [])
            done[0] += 1
            if not quiet and done[0] % 60 == 0:
                log("      %d/%d" % (done[0], len(channels)))
    progs = [p for p in progs if p["start"] and p["end"]]
    if not quiet:
        log("      节目 %d 条" % len(progs))
    return progs


def collect(districts, days, hist_days=7):
    chs, seen, prs_list = [], set(), []
    for d in districts:
        ab = load_ability(d)
        sub = build_channels(ab, d)
        fresh = []
        for c in sub:
            if c["uuid"] in seen:
                continue
            seen.add(c["uuid"])
            fresh.append(c)
            chs.append(c)
        if fresh:
            prs_list.extend(build_programs(fresh, ab, hist_days, days, quiet=True))
    prs, pk = [], set()
    for p in sorted(prs_list, key=lambda v: v["start"]):
        k = (p["uuid"], p["start"])
        if k not in pk:
            pk.add(k)
            prs.append(p)
    log("      合计 %d 频道 / %d 条节目（%d 个地市）" % (len(chs), len(prs), len(districts)))
    return chs, prs


def gkey(ch):
    g = ch["group"]
    rank = GROUP_ORDER.index(g) if g in GROUP_ORDER else len(GROUP_ORDER)
    sub = DNAME.get(ch["district"], "") if g == "地方" else ""
    return (rank, sub, int(re.sub(r"\D", "", ch["num"]) or 0), ch["name"])


def write_outputs(out, cdn, channels, programs, backup_days, days, base_url="", minimal=False):
    os.makedirs(out, exist_ok=True)
    now = datetime.now(TZ8)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    nowts = int(time.time())
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    epg_lo = int((day0 - timedelta(days=5)).timestamp())   # 节目单下界：前 5 天 00:00
    epg_hi = int((day0 + timedelta(days=2)).timestamp())   # 节目单上界：明天 24:00（含今天+明天）

    def write(name, text):
        tmp = os.path.join(out, name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, os.path.join(out, name))

    def live_of(ch):
        return "%s/%s/live1/%s/%s/1.m3u8" % (cdn, ch["cp"], ch["code"], ch["code"])

    def back_of(ch, va="{start}", vb="{end}"):
        return "%s/%s/lookback/%s/%s?playseek=%s-%s" % (cdn, ch["cp"], ch["code"], ch["code"], va, vb)

    def dlabel(ch):
        return DNAME.get(ch["district"], "") if ch["group"] == "地方" else "全省"


    fit = sorted(channels, key=gkey)

    # ---- 精简模式：只出 3 个文件 iptv.m3u8 / epg.xml / tvbox.json（外加 meta.json）----
    if minimal:
        base = (base_url or "").rstrip("/")
        _s, _f, va, vb = STYLES[0]
        tvg = (' url-tvg="%s/epg.xml" x-tvg-url="%s/epg.xml"' % (base, base)) if base else ""
        lines = ["#EXTM3U" + tvg,
                 "# 江苏移动 IPTV｜更新: %s｜节目单: 前5天~明天｜%d 频道"
                 % (stamp, len(channels))]
        for ch in fit:
            catch = ""
            if ch["back"] > 0:
                catch = (' catchup="append" catchup-days="%d" catchup-source="%s"'
                         % (backup_days, back_of(ch, va, vb)))
            lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-chno="%s" tvg-logo="%s" group-title="%s"%s,%s'
                         % (ch["uuid"], ch["name"], ch["num"], ch["logo"],
                            ch["group"] if ch["group"] != "地方" else "地方-%s" % DNAME.get(ch["district"], ""),
                            catch, ch["name"]))
            lines.append(live_of(ch))
        write("iptv.m3u8", "\n".join(lines))

        x = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="jsmcc-iptv-updater">']
        for ch in channels:
            icon = '<icon src="%s"/>' % escape(ch["logo"]) if ch["logo"] else ""
            x.append('  <channel id="%s"><display-name>%s</display-name>%s</channel>'
                     % (escape(str(ch["uuid"])), escape(ch["name"]), icon))
        for p in programs:
            if p["end"] <= epg_lo or p["start"] >= epg_hi:   # 前5天 00:00 ~ 明天 24:00
                continue
            s = datetime.fromtimestamp(p["start"], TZ8).strftime("%Y%m%d%H%M%S") + " +0800"
            e = datetime.fromtimestamp(p["end"], TZ8).strftime("%Y%m%d%H%M%S") + " +0800"
            x.append('  <programme start="%s" stop="%s" channel="%s"><title lang="zh">%s</title></programme>'
                     % (s, e, escape(str(p["uuid"])), escape(p["title"])))
        x.append("</tv>")
        write("epg.xml", "\n".join(x))

        write("tvbox.json", json.dumps({
            "lives": [{"name": "江苏移动IPTV", "type": 0,
                       "url": base + "/iptv.m3u8", "playerType": 1}],
            "epg": [base + "/epg.xml"],
            "epg_url": base + "/epg.xml",
        }, ensure_ascii=False, indent=2))

        write(".meta.json", json.dumps({
            "updated": stamp, "cdn": cdn, "channels": len(channels),
            "with_catchup": sum(1 for c in channels if c["back"] > 0),
            "programs": len(programs), "backup_days": backup_days,
            "program_days": days, "mode": "minimal",
        }, ensure_ascii=False, indent=2))
        return

    # 1. 三种 m3u
    texts = {}
    for _s, fname, va, vb in STYLES:
        lines = ["#EXTM3U",
                 "# 江苏移动 IPTV 直播源｜更新: %s｜节目单: 前5天~明天" % stamp,
                 "# 全江苏 %d 频道｜回看 %d 个｜使用说明见 README.md"
                 % (len(channels), sum(1 for c in channels if c["back"] > 0))]
        for ch in fit:
            catch = ""
            if ch["back"] > 0:
                catch = (' catchup="append" catchup-days="%d" catchup-source="%s"'
                         % (backup_days, back_of(ch, va, vb)))
            lines.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-chno="%s" tvg-logo="%s" group-title="%s"%s,%s'
                         % (ch["uuid"], ch["name"], ch["num"], ch["logo"],
                            ch["group"] if ch["group"] != "地方" else "地方-%s" % DNAME.get(ch["district"], ""),
                            catch, ch["name"]))
            lines.append(live_of(ch))
        texts[fname] = "\n".join(lines)
        write(fname, texts[fname])

    # 1b. 固定入口文件名（播放器/机顶盒只需要记住这两个，内容每次自动变）
    write("iptv.m3u8", texts["直播源.m3u"])                    # DIYP / 影视仓 / TiviMate 通用
    write("iptv_fongmi.m3u8", texts["直播源_tvbox_fongmi.m3u"])  # FongMi / TVBox 影视分支

    # 2. TVBox 原生 txt
    txt, cur = [], None
    for ch in fit:
        g = ch["group"] if ch["group"] != "地方" else "地方-%s" % DNAME.get(ch["district"], "")
        if g != cur:
            cur = g
            txt.append("%s,#genre#" % g)
        txt.append("%s,%s" % (ch["name"], live_of(ch)))
    write("直播源.txt", "\n".join(txt) + "\n")

    # 3. TVBoxOS JSON
    groups = {}
    for ch in fit:
        g = ch["group"] if ch["group"] != "地方" else "地方-%s" % DNAME.get(ch["district"], "")
        groups.setdefault(g, []).append({"name": ch["name"], "urls": [live_of(ch)]})
    write("直播源.json", json.dumps([{"group": g, "channels": v} for g, v in groups.items()],
                                    ensure_ascii=False, indent=1))

    # 4. 组播
    mm = ["#EXTM3U"]
    for ch in fit:
        if "udp://" in (ch["mc"] or ""):
            mm.append('#EXTINF:-1 tvg-chno="%s" group-title="%s",%s' % (ch["num"], ch["group"], ch["name"]))
            mm.append(ch["mc"])
    write("组播源.m3u", "\n".join(mm) + "\n")

    # 5. EPG.xml
    x = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="jsmcc-iptv-updater">']
    for ch in channels:
        icon = '<icon src="%s"/>' % escape(ch["logo"]) if ch["logo"] else ""
        x.append('  <channel id="%s"><display-name>%s</display-name>%s</channel>'
                 % (escape(str(ch["uuid"])), escape(ch["name"]), icon))
    for p in programs:
        if p["end"] <= epg_lo or p["start"] >= epg_hi:   # 前5天 00:00 ~ 明天 24:00（一周窗口）
            continue
        s = datetime.fromtimestamp(p["start"], TZ8).strftime("%Y%m%d%H%M%S") + " +0800"
        e = datetime.fromtimestamp(p["end"], TZ8).strftime("%Y%m%d%H%M%S") + " +0800"
        x.append('  <programme start="%s" stop="%s" channel="%s"><title lang="zh">%s</title></programme>'
                 % (s, e, escape(str(p["uuid"])), escape(p["title"])))
    x.append("</tv>")
    write("EPG.xml", "\n".join(x))

    # 6. 频道清单
    with open(os.path.join(out, "频道清单.csv.tmp"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["num", "name", "group", "district", "uuid", "code", "cp", "back", "mc"])
        w.writeheader()
        for ch in fit:
            w.writerow({"num": ch["num"], "name": ch["name"], "group": ch["group"],
                        "district": dlabel(ch), "uuid": ch["uuid"],
                        "code": ch["code"], "cp": ch["cp"], "back": ch["back"], "mc": ch["mc"]})
    os.replace(os.path.join(out, "频道清单.csv.tmp"), os.path.join(out, "频道清单.csv"))

    # 7. 节目源对照（频道 <-> 直播地址 <-> 回看模板 <-> 节目数）
    pc = {}
    for p in programs:
        pc.setdefault(p["uuid"], []).append(p)
    with open(os.path.join(out, "节目源对照.csv.tmp"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["分组", "频道名", "地区", "流代码", "CP", "支持回看", "节目数",
                    "节目单时间跨度", "直播地址", "回看地址模板(替换start-end)"])
        for ch in fit:
            ps = pc.get(ch["uuid"], [])
            span = ""
            if ps:
                span = "%s ~ %s" % (datetime.fromtimestamp(ps[0]["start"], TZ8).strftime("%m-%d %H:%M"),
                                    datetime.fromtimestamp(ps[-1]["start"], TZ8).strftime("%m-%d %H:%M"))
            w.writerow([ch["group"] if ch["group"] != "地方" else "地方-%s" % DNAME.get(ch["district"], ""),
                        ch["name"], dlabel(ch), ch["code"], ch["cp"],
                        "是" if ch["back"] else "否", len(ps), span, live_of(ch),
                        back_of(ch) if ch["back"] else ""])
    os.replace(os.path.join(out, "节目源对照.csv.tmp"), os.path.join(out, "节目源对照.csv"))

    # 8. 回看清单（已播节目 + 可直接播的示例地址）
    nowts = int(time.time())
    with open(os.path.join(out, "回看清单.csv.tmp"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["分组", "频道名", "回看天数", "近%d天已播节目数" % backup_days, "最长节目(分钟)", "可回看示例（直接可用）"])
        for ch in fit:
            if not ch["back"]:
                continue
            ps = [p for p in pc.get(ch["uuid"], []) if p["end"] < nowts]
            if not ps:
                continue
            longest = max(ps, key=lambda p: p["end"] - p["start"])
            last = sorted(ps, key=lambda p: p["end"])[-1]
            demo = "%s/%s/lookback/%s/%s?playseek=%s-%s" % (
                cdn, ch["cp"], ch["code"], ch["code"],
                datetime.fromtimestamp(last["start"], TZ8).strftime("%Y%m%d%H%M%S"),
                datetime.fromtimestamp(last["end"], TZ8).strftime("%Y%m%d%H%M%S"))
            w.writerow([ch["group"] if ch["group"] != "地方" else "地方-%s" % DNAME.get(ch["district"], ""),
                        ch["name"], backup_days, len(ps), int((longest["end"] - longest["start"]) / 60), demo])
    os.replace(os.path.join(out, "回看清单.csv.tmp"), os.path.join(out, "回看清单.csv"))

    # 8b. 机顶盒顺序对照：一个频道一行，按频道号排（中央1 -> 中央2 -> …）
    by_box = sorted(channels, key=lambda c: (int(re.sub(r"\D", "", c["num"]) or 0), c["name"]))
    past_by = {}
    for p in programs:
        if p["end"] < nowts and p["start"] >= nowts - backup_days * 86400:
            past_by.setdefault(p["uuid"], []).append(p)
    with open(os.path.join(out, "频道对照_机顶盒顺序.csv.tmp"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["机顶盒序号", "频道名", "分组", "地区", "直播源", "回看模板", "可回看节目数(近%d天)" % backup_days])
        for ch in by_box:
            w.writerow([ch["num"], ch["name"],
                        ch["group"] if ch["group"] != "地方" else "地方-%s" % DNAME.get(ch["district"], ""),
                        dlabel(ch), live_of(ch), back_of(ch) if ch["back"] else "",
                        len(past_by.get(ch["uuid"], [])) if ch["back"] else 0])
    os.replace(os.path.join(out, "频道对照_机顶盒顺序.csv.tmp"),
               os.path.join(out, "频道对照_机顶盒顺序.csv"))

    # 8c. 回看节目单：每个已播节目一行，地址直接用（近 backup_days 天）
    with open(os.path.join(out, "回看节目单.csv.tmp"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["机顶盒序号", "频道名", "节目名", "开始", "结束", "回看地址(可直接播)"])
        for ch in by_box:
            if not ch["back"]:
                continue
            for p in sorted(past_by.get(ch["uuid"], []), key=lambda v: -v["start"]):
                w.writerow([ch["num"], ch["name"], p["title"],
                            datetime.fromtimestamp(p["start"], TZ8).strftime("%Y-%m-%d %H:%M"),
                            datetime.fromtimestamp(p["end"], TZ8).strftime("%H:%M"),
                            "%s/%s/lookback/%s/%s?playseek=%s-%s" % (
                                cdn, ch["cp"], ch["code"], ch["code"],
                                datetime.fromtimestamp(p["start"], TZ8).strftime("%Y%m%d%H%M%S"),
                                datetime.fromtimestamp(p["end"], TZ8).strftime("%Y%m%d%H%M%S"))])
    os.replace(os.path.join(out, "回看节目单.csv.tmp"), os.path.join(out, "回看节目单.csv"))

    # 8d. TVBox 配置（一个地址同时带直播 + EPG，解决"两个地址分开填"）
    base = (base_url or "").rstrip("/")
    cfg = {
        "lives": [
            {"name": "江苏移动·回看版", "type": 0, "url": base + "/iptv.m3u8", "playerType": 1},
            {"name": "江苏移动·纯直播", "type": 1, "url": base + "/直播源.txt", "playerType": 1},
        ],
        "epg": [base + "/EPG.xml"],
        "epg_url": base + "/EPG.xml",
    }
    write("tvbox.json", json.dumps(cfg, ensure_ascii=False, indent=2))

    # 9. README（给分享对象看的）
    gcount = {}
    for ch in fit:
        g = ch["group"] if ch["group"] != "地方" else "地方-%s" % DNAME.get(ch["district"], "")
        gcount[g] = gcount.get(g, 0) + 1
    lines = [
        "# 江苏移动 IPTV 直播源（%d 频道｜%d 个支持回看）" % (len(channels), sum(1 for c in channels if c["back"] > 0)),
        "",
        "- 生成时间：%s（每天 %02d:%02d 自动重建）" % (stamp, _run_at()[0], _run_at()[1]),
        "- 适用范围：**江苏移动家庭宽带 / 移动手机网络**（非移动网络不可播）",
        "- 节目单：近 %d 天，共 %d 条；回看窗口：最近 %d 天" % (days, len(programs), backup_days),
        "",
        "## 文件说明",
        "",
        "| 文件 | 用途 |",
        "|---|---|",
        "| `直播源.txt` | TVBox 直接填，纯直播最稳（type=1） |",
        "| `iptv.m3u8` | **固定入口**，内容每天自动变，播放器只需记住这个地址 |",
        "| `iptv_fongmi.m3u8` | 固定入口 · FongMi 回看变量版 |",
        "| `tvbox.json` | **一条地址同时给出直播源 + EPG** |",
        "| `直播源_tvbox_fongmi.m3u` | FongMi / 影视仓等分支，带回看 |",
        "| `直播源.m3u` | DIYP / TiviMate / 影视仓，回看变量 `{start}` |",
        "| `直播源_kodi.m3u` | Kodi PVR IPTV Simple |",
        "| `直播源.json` | TVBoxOS 等分支的频道数组 |",
        "| `EPG.xml` | XMLTV 节目单 |",
        "| `节目源对照.csv` | 频道 ↔ 直播地址 ↔ 回看地址模板 ↔ 节目数 |",
        "| `回看清单.csv` | 哪些频道能回看 + 可直接播的示例地址 |",
        "| `组播源.m3u` | 组播（需 IPTV 承载网，普通宽带不通） |",
        "",
        "## 分组统计",
        "",
        "| 分组 | 频道数 |",
        "|---|---|",
    ]
    for g in sorted(gcount, key=lambda k: (GROUP_ORDER.index(k.split("-")[0]) if k.split("-")[0] in GROUP_ORDER else 9, k)):
        lines.append("| %s | %d |" % (g, gcount[g]))
    lines += [
        "",
        "## 怎么用",
        "",
        "1. TVBox 里「直播源」填 `直播源.txt` 的地址，「EPG」填 `EPG.xml`",
        "2. 想回看就用 `直播源_tvbox_fongmi.m3u`（你的播放器要认 `${(b)…}` 变量，节目单上会出回看图标）",
        "3. 回看单个节目：把 `节目源对照.csv` 里回看模板的 `{start}-{end}` 换成节目起止时间（北京时间 YYYYMMDDHHMMSS）",
        "",
        "## 说明",
        "",
        "- 直播/回看地址按**来源 IP** 放行，不含任何账号、令牌、MAC、机顶盒绑定信息，换设备换播放器都能用",
        "- 唯一依赖：江苏移动网络 + 静态设备分组号（已内置）",
        "- 本文件不含任何个人手机号、地址、设备标识",
        "- 仅供个人自用，请勿公开大规模传播（CDN 有并发限制）",
    ]
    write("README.md", "\n".join(lines) + "\n")

    write("meta.json", json.dumps({
        "updated": stamp, "cdn": cdn, "channels": len(channels),
        "with_catchup": sum(1 for c in channels if c["back"] > 0),
        "multicast": sum(1 for c in channels if "udp://" in (c["mc"] or "")),
        "programs": len(programs), "backup_days": backup_days, "program_days": days,
    }, ensure_ascii=False, indent=2))


# ---------- 自检与发布 ----------

def probe_live(url, timeout=8):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.status == 200 and "#EXTM3U" in r.read(300).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return False


def validate(out, channels, minimal=False):
    """抽样实测：直播 + 回看。返回 (ok, 明细)"""
    rnd = random.Random(2026)
    # 直播：CCTV-1/卫视/江苏/地方/其他 各抽几个
    picks, seen = [], {}
    for ch in channels:
        k = ch["group"]
        if seen.get(k, 0) < 4:
            seen[k] = seen.get(k, 0) + 1
            picks.append(ch)
    sample = picks + rnd.sample(channels, min(10, len(channels)))
    meta = json.loads(open(os.path.join(out, ".meta.json" if minimal else "meta.json"), encoding="utf-8").read())
    cdn = meta["cdn"]

    def one(ch):
        return probe_live("%s/%s/live1/%s/%s/1.m3u8" % (cdn, ch["cp"], ch["code"], ch["code"]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        live_ok = sum(1 for r in ex.map(one, sample) if r)

    # 回看：从刚生成的 回看清单.csv 里抽 5 条示例地址直接打（精简模式无此文件，跳过）
    back_rows = []
    fn = os.path.join(out, "回看清单.csv")
    if not minimal and os.path.exists(fn):
        back_rows = list(csv.DictReader(open(fn, encoding="utf-8-sig")))
    back_sample = rnd.sample(back_rows, min(5, len(back_rows))) if back_rows else []
    back_ok = sum(1 for r in back_sample if probe_live(r["可回看示例（直接可用）"], 12))

    # 隐私扫描
    leaks = []
    for fn in os.listdir(out):
        if not fn.endswith((".m3u", ".txt", ".json", ".csv", ".md", ".xml")):
            continue
        txt = open(os.path.join(out, fn), encoding="utf-8", errors="ignore").read()
        for pat, name in PRIVACY_PATTERNS:
            if re.search(pat, txt):
                leaks.append("%s: %s" % (fn, name))
    log("[4/5] 自检 直播 %d/%d，回看 %s，隐私 %s"
        % (live_ok, len(sample),
           ("%d/%d" % (back_ok, len(back_sample))) if back_sample else "跳过(精简模式)",
           "干净" if not leaks else leaks))
    ok = (live_ok >= max(3, int(len(sample) * 0.8))
          and (not back_sample or back_ok >= max(2, int(len(back_sample) * 0.6)))
          and not leaks)
    return ok, {"live": "%d/%d" % (live_ok, len(sample)),
                "catchup": ("%d/%d" % (back_ok, len(back_sample))) if back_sample else "skip",
                "privacy": "clean" if not leaks else ";".join(leaks)}


def publish(build, out, keep=None):
    for fn in os.listdir(build):
        os.replace(os.path.join(build, fn), os.path.join(out, fn))
    shutil.rmtree(build, ignore_errors=True)
    if not keep:
        return
    # 精简模式：www 是专属数据目录，保证里面只剩这 3 个（+.meta.json）
    # 其它目录则只清"旧版全套模式"留下的已知数据文件，绝不碰脚本/日志
    dedicated = os.path.basename(os.path.normpath(out)).lower() in ("www", "data", "output")
    if dedicated:
        keep_cf = {k.casefold() for k in keep}
        for fn in os.listdir(out):
            p = os.path.join(out, fn)
            if os.path.isfile(p) and fn.casefold() not in keep_cf:
                try:
                    os.remove(p)
                except OSError:
                    pass
        return
    stale = ("直播源.txt", "直播源.m3u", "直播源_kodi.m3u", "直播源_tvbox_fongmi.m3u",
             "直播源.json", "EPG.xml", "iptv_fongmi.m3u8", "README.md", "meta.json",
             "频道清单.csv", "节目源对照.csv", "回看清单.csv",
             "频道对照_机顶盒顺序.csv", "回看节目单.csv", "组播源.m3u")
    keep_cf = {k.casefold() for k in keep}
    for fn in stale:
        if fn.casefold() in keep_cf:
            continue
        p = os.path.join(out, fn)
        if not os.path.isfile(p):
            continue
        try:
            os.remove(p)
        except OSError:
            pass


def run_once(out, days, backup_days, districts, hist_days=7, base_url="", minimal=False):
    chs, prs = collect(districts, days, hist_days)
    if not chs:
        raise RuntimeError("一个频道都没拿到，保留上一版")
    nlogo = localize_logos(chs, base_url)
    if base_url:
        log("[台标] 本地化 %d/%d（地址: %s/logo/）" % (nlogo, len(chs), base_url.rstrip("/")))
    build = os.path.join(out, ".build")
    shutil.rmtree(build, ignore_errors=True)
    cache = os.path.join(HERE, "cdn_host.txt")
    last = None
    for h in ordered_hosts(cache):
        cdn = "http://" + h
        log("[3/5] 用 CDN %s 生成并自检 ..." % h)
        write_outputs(build, cdn, chs, prs, backup_days, days, base_url, minimal)
        ok, detail = validate(build, chs, minimal)
        if ok:
            meta = json.loads(open(os.path.join(build, ".meta.json" if minimal else "meta.json"), encoding="utf-8").read())
            meta["check"] = detail
            meta["status"] = "ok"
            with open(os.path.join(build, ".meta.json" if minimal else "meta.json"), "w", encoding="utf-8", newline="\n") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            publish(build, out, ({"iptv.m3u8", "epg.xml", "tvbox.json", ".meta.json"} if minimal else None))
            try:
                with open(cache, "w", encoding="utf-8") as f:
                    f.write(h)
            except Exception:  # noqa: BLE001
                pass
            log("[5/5] 发布完成 -> %s（%s）" % (out, detail))
            return meta
        last = detail
        log("      CDN %s 未通过自检（%s），换下一个" % (h, detail))
    shutil.rmtree(build, ignore_errors=True)
    raise RuntimeError("所有 CDN 均未通过自检（最后: %s），保留上一版文件" % last)


# ---------- 服务与定时 ----------

def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("223.110.247.231", 6610))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


ALLOW = {"iptv.m3u8", "epg.xml", "tvbox.json", "iptv_fongmi.m3u8", "EPG.xml",
         "直播源.txt", "直播源.m3u", "直播源_kodi.m3u", "直播源_tvbox_fongmi.m3u", "直播源.json",
         "README.md", "频道清单.csv", "节目源对照.csv", "回看清单.csv",
         "频道对照_机顶盒顺序.csv", "回看节目单.csv", "组播源.m3u"}


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        name = urllib.parse.unquote(self.path.split("?")[0]).lstrip("/")
        if name.startswith("logo/"):          # 本地台标
            self.send_logo(name[5:])
            return
        if name not in ALLOW:   # 输出与脚本同目录，只放行数据文件，避免把脚本/日志暴露出去
            self.send_error(404, "Not Found")
            return
        super().do_GET()

    def send_logo(self, fn):
        if not re.match(r"^[0-9a-f]{12}\.png$", fn):
            self.send_error(404, "Not Found")
            return
        p = os.path.join(LOGO_DIR, fn)
        if not os.path.isfile(p):
            self.send_error(404, "Not Found")
            return
        data = open(p, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def list_directory(self, path):
        self.send_error(404, "Not Found")
        return None

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def log_message(self, *a):
        pass


def serve(out, port):
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    h = functools.partial(Handler, directory=out)
    with socketserver.ThreadingTCPServer(("0.0.0.0", port), h) as srv:
        ip = lan_ip()
        log("[服务] 已启动，TVBox 里填：")
        print("\n     ① 一条地址（直播+回看+节目单）  http://%s:%d/tvbox.json" % (ip, port))
        print("     ② 直播+回看                     http://%s:%d/iptv.m3u8" % (ip, port))
        print("     ③ 节目单                        http://%s:%d/epg.xml\n" % (ip, port))
        srv.serve_forever()


def scheduler(out, days, backup_days, districts, hist_days=7, base_url="", minimal=False):
    at = _run_at()
    while True:
        now = datetime.now(TZ8)
        target = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        gap = (target - now).total_seconds()
        log("[定时] 下次更新 %s（%.1f 小时后）" % (target.strftime("%m-%d %H:%M"), gap / 3600))
        time.sleep(gap + 20)
        log("=== 定时更新开始 ===")
        try:
            run_once(out, days, backup_days, districts, hist_days, base_url, minimal)
        except Exception as e:      # noqa: BLE001
            log("!! 本次更新失败: %s（旧文件继续可用）" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "www"), help="输出目录，默认脚本目录下的 www")
    ap.add_argument("--days", type=int, default=3, help="节目单往后抓几天")
    ap.add_argument("--history-days", type=int, default=7, help="往前抓几天（用于回看节目单）")
    ap.add_argument("--backup-days", type=int, default=7)
    ap.add_argument("--port", type=int, default=12123)
    ap.add_argument("--base-url", default="", help="对外访问地址，如 http://10.0.0.2（写进 tvbox.json）")
    ap.add_argument("--district", default="all", help="地市码，逗号分隔；all=全江苏（默认）")
    ap.add_argument("--discover", action="store_true", help="扫描 CDN 网段找回可用 IP 后退出")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--minimal", action="store_true", help="只输出 iptv.m3u8 / epg.xml / tvbox.json")
    args = ap.parse_args()

    if args.discover:
        discover()
        return

    v = (args.district or "all").strip()
    districts = [c for c, _ in JIANGSU] if v.lower() == "all" else [x.strip() for x in v.split(",") if x.strip()]
    out = os.path.abspath(args.out)
    base = args.base_url or os.environ.get("BASE_URL", "")
    if not base and not args.once:
        ip = lan_ip()
        base = "http://%s" % ip if args.port == 80 else "http://%s:%d" % (ip, args.port)
    try:
        run_once(out, args.days, args.backup_days, districts, args.history_days, base, args.minimal)
    except Exception as e:          # noqa: BLE001
        log("!! 更新失败: %s" % e)
        if args.once:
            raise
        log("继续用上一版文件开服务")
    if args.once:
        return
    threading.Thread(target=scheduler,
                     args=(out, args.days, args.backup_days, districts, args.history_days, base, args.minimal),
                     daemon=True).start()
    serve(out, args.port)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止")
