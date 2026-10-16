const fs = require('fs'), vm = require('vm'), assert = require('assert');
const history = [
    {as_of:'2019-08-20',one_year:4.25,five_year:4.85},
    {as_of:'2026-09-20',one_year:3,five_year:3.5}
];
for (const touch of [false,true]) {
    const options = [];
    const chart = {setOption:option=>options.push(option)};
    const scope = {window:{matchMedia:()=>({matches:touch})}, document:{documentElement:{}},
        getComputedStyle:()=>({getPropertyValue:name=>name}),
        HP:{analysis:{},format:{pct:()=>{throw Error('LPR must not format as a change rate');}},
            chart:{create:()=>chart,axis:x=>x},dashComponents:{components:{}}}};
    vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/assets/js/dashboard/national_tab.js','utf8'),scope);
    assert.equal(scope.HP.lprChart({},history),chart);
    const base=options[0],lpr=options[1];
    assert.deepEqual(Array.from(base.xAxis.data),['2019-08','2026-09']);
    assert.deepEqual(Array.from(base.series[0].data),[4.25,3]);
    assert.deepEqual(Array.from(base.series[1].data),[4.85,3.5]);
    assert(base.series.every(s=>s.step==='end' && s.smooth===false && s.connectNulls===false));
    assert.equal(base.series[0].itemStyle.color,'--trend-up');
    assert.equal(base.series[1].itemStyle.color,'--tech-blue');
    assert.equal(base.dataZoom[1].height,touch?44:20);
    assert(lpr.dataZoom.every(z=>z.start===0 && z.end===100));
    const tooltip=lpr.tooltip.formatter([{dataIndex:1,marker:'',seriesName:'1年期 LPR',value:3}]);
    assert(tooltip.includes('3.00%') && !tooltip.includes('+3') && !tooltip.includes('2026-09-20'));
    assert.equal(lpr.yAxis.axisLabel.formatter(3.5),'3.50%');
    assert(!scope.HP.dashComponents.components.nationalTrends.template.includes('70城均价'));
    assert(scope.HP.dashComponents.components.nationalTrends.template.includes('LPR是银行贷款的参考利率'));
}
assert(!fs.readFileSync(__dirname+'/frontend/assets/js/dashboard/report_tab.js','utf8').includes('lpr-banner'));
console.log('LPR chart passed: term mapping, absolute percentages, step lines, full history and touch sizing.');
