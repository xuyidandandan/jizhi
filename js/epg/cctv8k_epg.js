
function main(item) {
    // CCTV-8K EPG 文件格式
    let url = "http://epg.cdn.loc.cc/";
    let channel = "cctv8k";
    let channelName = "CCTV-8K";

    let programs = [];
    let seen = {};

    let now = new Date();

    // 获取北京时间日期
    let bjNow = new Date(now.getTime() + 8 * 60 * 60 * 1000);

    // 前6天 + 今天 + 后1天
    for (let offset = -6; offset <= 1; offset++) {
        let date = new Date(Date.UTC(
            bjNow.getUTCFullYear(),
            bjNow.getUTCMonth(),
            bjNow.getUTCDate() + offset
        ));

        let year = date.getUTCFullYear();
        let month = ("0" + (date.getUTCMonth() + 1)).slice(-2);
        let day = ("0" + date.getUTCDate()).slice(-2);
        let dateText = year + "-" + month + "-" + day;

        let requestUrl = url + "?ch=" + channel + "&date=" + dateText;

        let res = jz.get(requestUrl, {
            "User-Agent": "Mozilla/5.0"
        });

        if (!res) continue;

        let json;

        try {
            json = typeof res === "string" ? JSON.parse(res) : res;
        } catch (e) {
            continue;
        }

        if (!json || json.code != 200 ||
            !Array.isArray(json.epg_data)) {
            continue;
        }

        let list = json.epg_data;

        // 当天北京时间零点对应的 Unix 毫秒时间戳
        let dayStart = Date.UTC(
            year,
            date.getUTCMonth(),
            date.getUTCDate()
        ) - 8 * 60 * 60 * 1000;

        for (let i = 0; i < list.length; i++) {
            let p = list[i];

            if (!p.start || !p.end || !p.title) continue;

            let title = String(p.title).trim();
            let startText = String(p.start).trim();
            let endText = String(p.end).trim();

            if (!title) continue;

            if (!/^\d{2}:\d{2}$/.test(startText) ||
                !/^\d{2}:\d{2}$/.test(endText)) {
                continue;
            }

            let startParts = startText.split(":");
            let endParts = endText.split(":");

            let sh = parseInt(startParts[0], 10);
            let sm = parseInt(startParts[1], 10);
            let eh = parseInt(endParts[0], 10);
            let em = parseInt(endParts[1], 10);

            if (sh > 23 || sm > 59 || eh > 23 || em > 59) {
                continue;
            }

            let start = dayStart + (sh * 60 + sm) * 60000;
            let stop = dayStart + (eh * 60 + em) * 60000;

            // 跨午夜节目
            if (stop <= start) {
                stop += 24 * 60 * 60 * 1000;
            }

            // 按节目名称和开始时间去重
            let key = title + "|" + start;

            if (seen[key]) continue;
            seen[key] = true;

            programs.push({
                name: channelName,
                title: title,
                desc: p.desc || "",
                start: start,
                stop: stop
            });
        }
    }

    // 按开始时间排序
    programs.sort(function(a, b) {
        return a.start - b.start;
    });

    // 文件格式必须返回 programs
    return {
        programs: programs
    };
}
