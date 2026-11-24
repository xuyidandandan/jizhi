function main(item) {
    const now = new Date(); // 当前时间
    const tomorrow = new Date(now.getTime() + 24 * 60 * 60 * 1000); // 明天
    const timestamps = [];

    // 生成从明天开始的连续 8 天
    for (let i = 0; i < 8; i++) {
        const date = new Date(tomorrow.getTime() - i * 24 * 60 * 60 * 1000);
        date.setHours(0, 0, 0, 0); // 设置为当天 00:00:00
        timestamps.push(date.getTime());
    }

    const channels = [
        { name: "CCTV1", id: "cctv1" },
        { name: "CCTV2", id: "cctv2" },
        { name: "CCTV3", id: "cctv3" },
        { name: "CCTV4", id: "cctv4" },
        { name: "CCTV5", id: "cctv5" },
        { name: "CCTV5+", id: "cctv5plus" },
        { name: "CCTV6", id: "cctv6" },
        { name: "CCTV7", id: "cctv7" },
        { name: "CCTV8", id: "cctv8" },
        { name: "CCTV9", id: "cctvjilu" },
        { name: "CCTV10", id: "cctv10" },
        { name: "CCTV11", id: "cctv11" },
        { name: "CCTV12", id: "cctv12" },
        { name: "CCTV13", id: "cctv13" },
        { name: "CCTV14", id: "cctvchild" },
        { name: "CCTV15", id: "cctv15" },
        { name: "CCTV16", id: "cctv16" },
        { name: "CCTV17", id: "cctv17" },
        { name: "CCTV4欧洲", id: "cctveurope" },
        { name: "CCTV4美洲", id: "cctvamerica" }
    ];

    const headers = {
        'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1 Edg/138.0.0.0'
    }

    const programs = [];
    for (const channel of channels) {
        for (const timestamp of timestamps) {
            const url = "https://api.cntv.cn/epg/getEpgInfoByChannelNew?c=" + channel.id + "&serviceId=tvcctv&d=" + jz.toDate(timestamp, "yyyyMMdd");
            const res = jz.get(url, headers);
            if (!jz.isJSONObject(res)) {
                continue;
            }
            const json = JSON.parse(res);
            const dataList = json.data[channel.id].list;

            if (!Array.isArray(dataList)) {
                continue;
            }
            for (const item of dataList) {
                programs.push({ name: channel.name, title: item.title, start: item.startTime * 1000, stop: item.endTime * 1000 });
            }
        }
    }

    return { programs: programs }
}
