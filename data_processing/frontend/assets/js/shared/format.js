/* 共享数字格式化工具（带空值守卫，模板中禁止直接调用 toLocaleString）。 */
window.HP = window.HP || {};

HP.format = (function () {
    function toNum(v) {
        if (v === null || v === undefined || v === '') return null;
        var n = Number(v);
        return isFinite(n) ? n : null;
    }
    return {
        priceCoverage: function (rows, month) {
            const derived=p=>['index_estimate','reference_estimate'].includes(p?.method);
            return {
                derived:rows.filter(r=>r.point.price>0&&derived(r.point)).length,
                observed:rows.filter(r=>r.point.price>0&&r.point.method==='listing_observation'&&r.point.evidence?.status==='traceable_listing').length,
                historical:rows.filter(r=>(r.cityData?.monthly||[]).some(p=>p.date<=month&&p.price>0&&derived(p))).length,
                missing:rows.filter(r=>r.value==null).map(r=>r.city)
            };
        },
        priceSource: function (p) {
            if(!p)return '暂无';
            if(p.method==='index_estimate'||p.method==='reference_estimate')return '逆推结构';
            if(p.method==='listing_observation'&&p.evidence?.status==='traceable_listing')return '安居客数据';
            return '暂无';
        },
        /** 价格/普通数字：51259 -> "51,259"；无效返回 "--" */
        price: function (v) {
            var n = toNum(v);
            return n === null ? '--' : Math.round(n).toLocaleString('zh-CN');
        },
        /** 万元：51259 -> "5.1万"；<10000 原样 */
        wan: function (v) {
            var n = toNum(v);
            if (n === null) return '--';
            return n >= 10000 ? (n / 10000).toFixed(1) + '万' : String(Math.round(n));
        },
        /** 百分比：2.31 -> "+2.31%"，-1.5 -> "-1.5%" */
        pct: function (v) {
            var n = toNum(v);
            if (n === null) return '--';
            if (Math.abs(n) < 0.005) return '0%';
            return (n > 0 ? '+' : '') + n.toFixed(2).replace(/\.?0+$/, '') + '%';
        },
        /** 符号判断：>0 → 'up'（红），<0 → 'down'（绿） */
        trend: function (v) {
            var n = toNum(v);
            if (n === null) return 'neutral';
            return n > 0 ? 'up' : n < 0 ? 'down' : 'neutral';
        },
        /** 中国市场：涨红跌绿，返回 CSS 变量字符串供内联 color 使用 */
        trendColor: function (v) {
            var tr = this.trend(v);
            if (tr === 'up') return 'var(--trend-up)';
            if (tr === 'down') return 'var(--trend-down)';
            return '';
        },
        /** CSS class: 涨 delta-up / 跌 delta-down */
        trendClass: function (v) {
            var tr = this.trend(v);
            if (tr === 'up') return 'delta-up';
            if (tr === 'down') return 'delta-down';
            return '';
        },
        num: toNum
    };
})();
