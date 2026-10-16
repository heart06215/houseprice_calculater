const fs = require('fs'), vm = require('vm'), assert = require('assert');
const investment = {label:'全国房地产开发投资额',title:'房地产开发投资',unit:'亿元',token:'--accent-ink'};
const starts = {label:'房屋新开工面积',title:'房屋新开工面积',unit:'万㎡',token:'--tech-blue'};
const history = [
    {month:'2025-12',period:'2025年1-12月',published_at:'2026-01-19',items:[{label:investment.label,value:82788,unit:'亿元',yoy:-17.2},{label:starts.label,value:58770,unit:'万㎡',yoy:-20.4}]},
    {month:'2026-02',period:'2026年1-2月',published_at:'2026-03-16',items:[{label:investment.label,value:9612,unit:'亿元',yoy:-11.1},{label:starts.label,value:5084,unit:'万㎡',yoy:-23.1}]},
    {month:'2026-04',period:'2026年1-4月',published_at:'2026-05-18',items:[{label:investment.label,value:23260,unit:'亿元',yoy:-13.7},{label:starts.label,value:13525,unit:'万㎡',yoy:-22}]}
];
for (const touch of [false,true]) {
    const options=[];
    const chart={setOption:option=>options.push(option)};
    const scope={Intl,window:{matchMedia:()=>({matches:touch})},document:{documentElement:{}},
        getComputedStyle:()=>({getPropertyValue:name=>name}),
        Vue:{inject:()=>({information:{value:{macro:{realestate:{history}}}}}),
            computed:get=>({get value(){return get();}}),shallowRef:value=>({value}),ref:value=>({value}),
            onMounted:()=>{},watch:()=>{},onBeforeUnmount:()=>{}},
        HP:{format:{},chart:{create:()=>chart,axis:x=>x},dashComponents:{components:{}}}};
    for (const name of ['analysis-data.js','national_tab.js','realestate_tab.js'])
        vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/assets/js/dashboard/'+name,'utf8'),scope);
    const state=scope.HP.dashComponents.components.realestateTrends.setup();
    assert.equal(state.mode.value,'yoy','Default trend must use official YoY, not the naturally increasing cumulative total');
    assert.equal(state.displayed.value.month,'2026-04');
    assert.equal(state.displayedItems.value[0].yoy,-13.7);
    assert.deepEqual(Array.from(state.gapMonths.value),['2026-03']);
    state.year.value='2025';
    assert.equal(state.displayed.value.month,'2025-12','Summary must follow the selected chart year');
    assert.equal(state.displayedItems.value[0].value,82788);
    assert.equal(state.displayedItems.value[1].yoy,-20.4);
    assert.equal(state.gapMonths.value.length,0,'Complete history must not display a red missing-report legend');
    state.year.value='2024';
    assert.equal(state.displayed.value,undefined,'Empty selected year cannot show another period\'s summary');
    const series=scope.HP.realestateSeries(history,investment,'value');
    assert.deepEqual(Array.from(series.months),['2025-12','2026-01','2026-02','2026-03','2026-04']);
    assert.deepEqual(Array.from(series.values),[82788,null,9612,null,23260]);
    const yoy=scope.HP.realestateSeries(history,starts,'yoy');
    assert.deepEqual(Array.from(yoy.months),['2025-12','2026-02','2026-03','2026-04']);
    assert.deepEqual(Array.from(yoy.values),[-20.4,-23.1,null,-22]);
    const crossYear=scope.HP.realestateSeries(history.slice(0,2),investment,'yoy');
    assert.deepEqual(Array.from(crossYear.values),[-17.2,-11.1],'December connects directly to the next combined January-February report');
    assert.equal(crossYear.rows.length,2,'No fabricated January record or interpolated value');
    assert.equal(scope.HP.realestateSeries([],investment,'value').values.length,0);
    assert.equal(scope.HP.realestateSeries([history[0],history[0]],investment,'value').values[0],null);
    const invalid=structuredClone(history[0]); invalid.items[1].unit='平方米';
    assert.equal(scope.HP.realestateSeries([invalid],starts,'value').values[0],null);
    scope.HP.realestateChart({},history,starts,'value');
    assert.deepEqual(Array.from(options[0].series[0].data),[58770,null,5084,null,13525]);
    assert.equal(options[0].series[0].connectNulls,false);
    assert.equal(options[0].series[0].smooth,false);
    assert.equal(options[0].series[0].itemStyle.color,'--tech-blue');
    assert.equal(options[0].series[0].lineStyle.color,'--tech-blue','Regular data lines must keep their own color');
    const areaGap=options[0].series[0].markLine;
    assert.equal(areaGap.lineStyle.color,'--trend-up');
    assert.equal(areaGap.lineStyle.type,'solid');
    assert.equal(areaGap.data.length,1,'Annual January reset must not create a missing-report bridge');
    assert.deepEqual(Array.from(areaGap.data[0][0].coord),['2026-02',5084]);
    assert.deepEqual(Array.from(areaGap.data[0][1].coord),['2026-04',13525]);
    assert(areaGap.tooltip.formatter({dataIndex:0}).includes('2026-03'));
    assert.equal(options[0].dataZoom[1].height,touch?44:20);
    assert.equal(options[1].yAxis.name,'累计（万㎡）');
    assert.equal(options[1].xAxis.inverse,false);
    assert.equal(options[1].yAxis.inverse,false);
    const tooltip=options[1].tooltip.formatter([{dataIndex:2}]);
    assert(tooltip.includes('2026年1-2月') && tooltip.includes('5,084 万㎡') && tooltip.includes('-23.1%') && tooltip.includes('2026-03-16'));
    assert.equal(options[1].tooltip.formatter([{dataIndex:1}]),'1—2月合并发布，无单独1月数据');
    assert(options[1].tooltip.formatter([{dataIndex:3}]).includes('报告缺失'));
    scope.HP.realestateChart({},history,investment,'yoy');
    assert.deepEqual(Array.from(options[2].series[0].data),[-17.2,-11.1,null,-13.7]);
    assert.equal(options[2].series[0].lineStyle.color,'--accent-ink');
    assert.equal(options[2].series[0].connectNulls,false,'Missing values stay null; only the red annotation bridges the endpoints');
    assert.deepEqual(Array.from(options[2].series[0].markLine.data[0][0].coord),['2026-02',-11.1]);
    assert.deepEqual(Array.from(options[2].series[0].markLine.data[0][1].coord),['2026-04',-13.7]);
    assert.equal(options[3].yAxis.name,'累计同比（%）');
    assert.equal(options[3].yAxis.axisLabel.formatter(-11.1),'-11.1%');
    assert(options[3].dataZoom.every(row=>row.start===0 && row.end===100));
    const positive=structuredClone(history[0]); positive.items[0].yoy=38.3;
    assert.equal(scope.HP.realestateSeries([positive],investment,'yoy').values[0],38.3,'Positive official growth must retain its sign too');
    const multiple=scope.HP.realestateGaps({months:['2026-02','2026-03','2026-04','2026-05','2026-06','2026-07','2026-08'],values:[10,null,null,20,null,30,null]});
    assert.deepEqual(JSON.parse(JSON.stringify(multiple)),[
        {from:0,to:3,months:['2026-03','2026-04']},{from:3,to:5,months:['2026-06']}
    ],'Consecutive gaps share a straight bridge; disjoint gaps are separate; no extrapolation after the last point');
    assert.equal(scope.HP.realestateGaps({months:['2026-02','2026-03','2026-04'],values:[null,10,null]}).length,0,'No bridge without valid points at both ends');
    const crossYearGap=scope.HP.realestateGaps({months:['2025-11','2025-12','2026-01','2026-02'],values:[100,null,null,10]});
    assert.deepEqual(JSON.parse(JSON.stringify(crossYearGap)),[{from:0,to:3,months:['2025-12']}],'A missing December is marked; nonreporting January is excluded');
}
console.log('Real-estate chart passed: combined January-February continuity, red straight gap bridges, single/multiple/year-boundary gaps, nulls retained, no extrapolation, units and summaries.');
