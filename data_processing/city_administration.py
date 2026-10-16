"""70-city provincial affiliations, independent of optional map coverage metadata.

Keys match NBS city names; values match MoHRSS provincial table labels.
Shenzhen remains Guangdong here; its separately published wage row wins in the UI.
"""
PROVINCE_CITIES = {
    '北京':'北京', '天津':'天津', '上海':'上海', '重庆':'重庆',
    '河北':'石家庄 唐山 秦皇岛', '山西':'太原', '内蒙古':'呼和浩特 包头',
    '辽宁':'沈阳 大连 丹东 锦州', '吉林':'长春 吉林', '黑龙江':'哈尔滨 牡丹江',
    '江苏':'南京 无锡 徐州 扬州', '浙江':'杭州 宁波 温州 金华',
    '安徽':'合肥 蚌埠 安庆', '福建':'福州 厦门 泉州', '江西':'南昌 九江 赣州',
    '山东':'济南 青岛 烟台 济宁', '河南':'郑州 洛阳 平顶山',
    '湖北':'武汉 宜昌 襄阳', '湖南':'长沙 岳阳 常德',
    '广东':'广州 深圳 韶关 湛江 惠州', '广西':'南宁 桂林 北海',
    '海南':'海口 三亚', '四川':'成都 泸州 南充', '贵州':'贵阳 遵义',
    '云南':'昆明 大理', '陕西':'西安', '甘肃':'兰州', '青海':'西宁',
    '宁夏':'银川', '新疆':'乌鲁木齐',
}
CITY_PROVINCES = {city:province for province,cities in PROVINCE_CITIES.items() for city in cities.split()}
