/* ECharts 5 统一样式工厂 + 图表实例管理。
   所有图表从这里取轴/tooltip/grid 配置，消除各 build 函数里的重复。 */
window.HP = window.HP || {};

HP.chart = (function () {
    var cText = '#1a3a42';
    var cLine = '#8aa8b0';
    var cSplit = '#c5d4d8';

    var registry = [];
    var observers = new WeakMap();
    echarts.registerTheme('housing-warm-glass', {
        animation: !window.matchMedia('(prefers-reduced-motion: reduce)').matches,
        animationDuration: 360, animationDurationUpdate: 240, animationEasing: 'cubicOut',
        color: ['#dc2626','#0aa0c8','#b91c1c','#c2410c','#176878'],
        textStyle: {fontFamily:'Montserrat, "Glow Sans SC", "Noto Sans SC", sans-serif',fontSize:15,color:'#111111'},
        legend: {textStyle:{fontSize:15,color:'#163e48',fontWeight:600}},
        tooltip: {backgroundColor:'#fff',borderColor:'#163e48',borderWidth:2,
            textStyle:{fontSize:16,color:'#111111'},extraCssText:'border-radius:4px;box-shadow:0 6px 22px #39332622;'},
        categoryAxis: {axisLine:{lineStyle:{color:'#0aa0c8',width:1.5}},axisLabel:{fontSize:15,color:'#163e48'}},
        valueAxis: {axisLabel:{fontSize:15,color:'#163e48'},splitLine:{lineStyle:{color:'#c5d4d8',type:'dashed'}}},
        dataZoom: {backgroundColor:'#f4f8f9',fillerColor:'rgba(10,160,200,.22)',borderColor:'#8aa8b0',
            handleStyle:{color:'#0aa0c8',borderColor:'#111111'},textStyle:{color:'#163e48'}}
    });

    function create(el) {
        var inst = echarts.init(el, 'housing-warm-glass');
        registry.push(inst);
        if (window.ResizeObserver) {
            var observer = new ResizeObserver(function () {
                if (inst.isDisposed()) return;
                if (el.clientWidth > 0 && el.clientHeight > 0 &&
                    (inst.getWidth() !== el.clientWidth || inst.getHeight() !== el.clientHeight)) inst.resize();
            });
            observer.observe(el);
            observers.set(inst, observer);
        }
        return inst;
    }

    function dispose(inst) {
        if (!inst) return;
        var i = registry.indexOf(inst);
        if (i >= 0) registry.splice(i, 1);
        var observer = observers.get(inst);
        if (observer) { observer.disconnect(); observers.delete(inst); }
        inst.dispose();
    }

    function resizeAll() {
        registry.forEach(function (c) { c && c.resize(); });
    }

    window.addEventListener('resize', resizeAll);

    /** 直角坐标系通用轴配置 */
    function axis(opts) {
        opts = opts || {};
        return Object.assign({
            type: opts.type || 'value',
            name: opts.name || '',
            nameTextStyle: { color: cText, fontSize: 15, fontWeight: 600 },
            data: opts.data,
            axisLabel: {
                color: cText,
                fontSize: 15,
                rotate: opts.rotate || 0,
                formatter: opts.formatter
            },
            axisLine: { lineStyle: { color: cLine, width: 1.25 } },
            splitLine: { show: opts.splitLine !== false, lineStyle: { color: cSplit } },
            logBase: opts.log ? 10 : undefined
        }, opts, {axisLabel: Object.assign({color: cText, fontSize: 14}, opts.axisLabel || {}, opts.rotate ? {rotate: opts.rotate} : {})});
    }

    function grid(opts) {
        opts = opts || {};
        return {
            left: opts.left != null ? opts.left : 60,
            right: opts.right != null ? opts.right : 30,
            top: opts.top != null ? opts.top : 40,
            bottom: opts.bottom != null ? opts.bottom : 50
        };
    }

    /** 中国地图 geojson：内联数据优先，否则本地文件 */
    function loadChinaGeo() {
        if (window.__CHINA_GEOJSON__) return Promise.resolve(window.__CHINA_GEOJSON__);
        return fetch('assets/vendor/china.geo.json').then(function (r) {
            if (!r.ok) throw new Error('中国地图数据加载失败: HTTP ' + r.status);
            return r.json();
        });
    }

    return {
        cText: cText, cLine: cLine, cSplit: cSplit,
        /* 中国市场涨红跌绿 */ lineColor: '#dc2626', upColor: '#dc2626', downColor: '#16a34a',
        axis: axis, grid: grid,
        create: create, dispose: dispose, resizeAll: resizeAll,
        loadChinaGeo: loadChinaGeo
    };
})();
