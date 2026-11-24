function main(item) {
    // 从URL获取频道ID参数
    let id = jz.getQuery(item.url, 'id') || 'bjjs';
    
    // 频道映射表
    const n = {
        'bjws': ['573ib1kp5nk92irinpumbo9krlb', "北京卫视"],
        'bjws4k': ['5755n511tbk8flo40l4c71l0sdf', "北京卫视4K超高清"],
        'bjwy': ['54db6gi5vfj8r8q1e6r89imd64s', "北京文艺"],
        'bjjs': ['53bn9rlalq08lmb8nf8iadoph0b', "北京纪实科教"],
        'bjys': ['50mqo8t4n4e8gtarqr3orj9l93v', "北京影视"],
        'bjcj': ['50e335k9dq488lb7jo44olp71f5', "北京财经"],
        'bjty': ['54hv0f3pq079d4oiil2k12dkvsc', "北京体育休闲"],
        'bjsh': ['50j015rjrei9vmp3h8upblr41jf', "北京生活"],
        'bjxw': ['53gpt1ephlp86eor6ahtkg5b2hf', "北京新闻"],
        'bjkk': ['55skfjq618b9kcq9tfjr5qllb7r', "北京卡酷少儿"]
    };
    
    // 验证id是否有效
    if (!n[id]) {
        return { error: `无效的频道ID: ${id}` };
    }
    
    // 获取对应的pid
    let pid = n[id][0];
    
    // 生成时间戳
    let t = Math.floor(Date.now() / 1000);
    let t2 = Date.now();
    
    // 计算签名
    let sign = jz.md5(pid + "151" + t + 'TtJSg@2g*$K4PjUH').slice(0, 8);
    
    // 构建请求URL
    let url = `https://pc.api.btime.com/video/play?from=pc&id=${pid}&type_id=151&timestamp=${t}&sign=${sign}&_=${t2}`;
    
    // 设置请求头
    let headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
        'Accept-Encoding': 'identity',
        'pragma': 'no-cache',
        'cache-control': 'no-cache',
        'Referer': 'https://www.btime.com/',
        'Cookie': 'usid=24458c107f1da4aa79342a24811d67b4; lf=1;'
    };
    
    // 发送GET请求
    let response = jz.get(url, headers);
    
    // 检查响应是否为空
    if (!response || response.length === 0) {
        return { error: "服务器返回空响应" };
    }
    
    // 处理jQuery前缀的响应
    let data = response;
    if (response.trim().startsWith('jQuery')) {
        let match = response.match(/\((.*?)\)/);
        if (match) {
            data = match[1];
        }
    }
    
    // 解析JSON
    let json;
    try {
        json = JSON.parse(data);
    } catch(e) {
        let preview = response.length > 200 ? response.substring(0, 200) : response;
        return { error: "JSON解析失败" };
    }
    
    // 检查错误码 - 只返回错误信息，不拼接频道名称
    if (json.errno !== 0) {
        let errmsg = json.errmsg || "获取地址失败";
        return { error: errmsg };
    }
    
    // 获取播放地址
    let stream_url = json.data.video_stream[0].stream_url;
    
    // 处理播放地址
    let play_url;
    if (stream_url.startsWith('http://') || stream_url.startsWith('https://')) {
        play_url = stream_url;
    } else {
        play_url = jz.decodeBase64(jz.decodeBase64(stream_url.split('').reverse().join('')));
    }
    
    // 返回播放地址
    return { url: play_url };
}
