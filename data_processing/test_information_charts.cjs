const fs = require('fs'), vm = require('vm'), assert = require('assert');
const options = {}, disposed = [];
let mount, unmount;
const prices = [
    {city:'甲', kind:'new_house', period:'2026-08', price:100},
    {city:'乙', kind:'new_house', period:'2026-07', price:200},
    {city:'甲', kind:'second_hand', period:'2026-08', price:150, mom_pct:1},
    {city:'乙', kind:'second_hand', period:'2026-08', price:240, mom_pct:-2},
    {city:'丙', kind:'second_hand', period:'2026-08', price:120, mom_pct:null},
    {city:'丙', kind:'new_house', period:'2026-08', price:60},
    {city:'甲', kind:'rent', period:'2026-07', price:8},
    {city:'乙', kind:'rent', period:'2026-07', price:3}
];
const supply_22 = {month:'2026-08',headers:['推出宗数(宗)','推出规划建面(万㎡)','成交楼面价(元/㎡)','溢价率(%)','成交规划建面(万㎡)'],aggregate:{values:[10,20,30,40,50]}};
const context = {information:{value:{cih:{prices,land:{sections:{supply_22}}}}}};
const scope = {
    URL, getComputedStyle:()=>({getPropertyValue:()=>''}),
    document:{documentElement:{},querySelector:selector=>selector},
    Vue:{inject:()=>context,ref:value=>({value}),reactive:value=>value,
        computed:fn=>({get value(){return fn();}}),watch:(source,fn)=>fn(source.value),
        onMounted:fn=>{mount=fn;},nextTick:fn=>fn(),onBeforeUnmount:fn=>{unmount=fn;}},
    HP:{format:{},dashComponents:{components:{}},chart:{
        create:el=>({setOption:value=>{options[el]=value;},el}),dispose:chart=>disposed.push(chart.el)
    }},echarts:{}
};
scope.window=scope;
vm.runInNewContext(fs.readFileSync(__dirname+'/frontend/assets/js/dashboard/information_tab.js','utf8'),scope);
const state=scope.HP.dashComponents.components.information.components.SourcePanel.setup();
const premium={scope:'300城宅地供求',period:'2026年01-10月',label:'平均溢价率',value:8.74,unit:'%',source_url:'https://www.cih-index.com/data/land.html'};
context.information.value.cih.metrics=[premium];
assert.equal(state.landBid.value.percent,108.74,'land ratio adds the 100% starting price base');
assert.equal(state.landBid.value.period,premium.period);
assert.equal(state.landBid.value.source_url,premium.source_url);
assert.equal(scope.HP.landBidRatio([{...premium,value:0}]).percent,100,'zero premium is a valid base-price deal');
assert.equal(scope.HP.landBidRatio([{...premium,value:-5}]).percent,95);
for (const patch of [{scope:'22城宅地供求'},{unit:'元/㎡'},{value:null},{value:NaN},{value:-100},
    {label:'成交楼面均价'},{period:'2026年10-01月'},{source_url:'https://example.com/data/land.html'}]) {
    assert.equal(scope.HP.landBidRatio([{...premium,...patch}]),null,'missing/wrong basis must not create a ratio');
}
assert.equal(scope.HP.landBidRatio([]),null);
assert.equal(scope.HP.landBidRatio([null]),null);
assert.equal(scope.HP.landBidRatio([{...premium,value:8.75},premium]),null,'conflicting observations cannot be averaged');
assert.equal(scope.HP.landBidRatio([{...premium,period:'2025年12月',value:10},premium]).percent,108.74,'choose latest source period numerically');
const houseRows=prices.map(row=>({...row,unit:'元/㎡',source_url:'https://www.cih-index.com/'}));
houseRows.push({...houseRows[0],city:'无地价城市',price:10000});
const floorSupply={period:'2026年01-08月',month:'2026-10',source_url:premium.source_url,
    headers:['成交土地面积单价(元/㎡)','成交楼面均价(元/㎡)'],
    cities:[{city:'甲',values:[100000,10]},{city:'丙',values:[100000,30]},{city:'乙',values:[100000,900]}]};
const ratio=scope.HP.houseLandRatio(houseRows,floorSupply);
assert.equal(ratio.ratio,4,'ratio of the same two city means, not mean of city ratios');
assert.equal(ratio.count,2,'exclude missing cities and older housing month');
assert.equal(ratio.house_price,80);assert.equal(ratio.land_price,20);assert.equal(ratio.land_share,25);
assert.equal(ratio.house_period,'2026-08');assert.equal(ratio.land_period,'2026年01-08月','refresh cursor is not statistical period');
for(const patch of [{period:null},{period:'2026年08-01月'},{period:'2025年01-08月'},
    {headers:['成交土地面积单价(元/㎡)']},{source_url:'https://example.com/data/land.html'},{cities:[]}]){
    assert.equal(scope.HP.houseLandRatio(houseRows,{...floorSupply,...patch}),null,'cannot silently substitute units, periods or sources');
}
assert.equal(scope.HP.houseLandRatio(houseRows.filter(r=>r.kind==='second_hand'),floorSupply),null,'resale cannot represent developer sales');
assert.equal(scope.HP.houseLandRatio(houseRows.map(r=>({...r,unit:'指数'})),floorSupply),null);
assert.equal(scope.HP.houseLandRatio(houseRows.map(r=>({...r,price:0})),floorSupply),null);
assert.equal(scope.HP.houseLandRatio(houseRows,null),null);
assert.equal(scope.HP.houseLandRatio([...houseRows,{...houseRows[0],price:110}],floorSupply),null,'conflicting city observation is rejected');
assert.equal(scope.HP.houseLandRatio(houseRows,{...floorSupply,cities:[...floorSupply.cities,{city:'甲',values:[0,11]}]}),null);
const unusableLand={...floorSupply,cities:floorSupply.cities.map(r=>({...r,values:[100,0]}))};
assert.equal(scope.HP.houseLandRatio(houseRows,unusableLand),null,'zero denominator never renders Infinity');
assert.equal(state.bothPrice.value.length,2,'prices pair only within city and month, without requiring a change rate');
assert.equal(state.bothPrice.value.find(r=>r.city==='甲').ratio,1.5);
assert.equal(state.bothPrice.value.find(r=>r.city==='丙').ratio,2);
assert(!state.bothPrice.value.some(r=>r.city==='乙'),'different-month new-house sample cannot pair');
assert.equal(state.rentPeriod.value,'2026-07','rental period is independent of second-hand period');
mount();
for (const [name,rows,field] of [['shDist',state.shRows.value,'mom_pct'],['rentRank',state.rentRows.value,'price']]) {
    const chart=options['[data-chart='+name+']'];
    chart.yAxis.data.forEach((city,index)=>assert.equal(chart.series[0].data[index]?.value??chart.series[0].data[index],rows.find(r=>r.city===city)[field],city+' bar value must match city label'));
}
const land=options['[data-chart=landBar]'];
assert.equal(land.xAxis.name,'万㎡');
assert.equal(land.series[0].data.length,2,'incompatible units do not share the area axis');
unmount();assert.equal(disposed.length,4,'all source charts disposed when leaving the view');
console.log('Information charts passed: city/value mapping, matching months, rental periods, land units, bid ratio basis/missing/conflicts and disposal.');
