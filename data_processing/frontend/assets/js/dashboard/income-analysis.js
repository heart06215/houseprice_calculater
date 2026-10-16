/* Pure 90-sqm price/income comparisons. Geography, dates and missing values stay explicit. */
(function(){
    const positive=v=>Number.isFinite(v)&&v>0;
    function priceSnapshot(cities,city,month){
        const selected=city?cities.filter(c=>c.city===city):cities;
        month=month||selected.flatMap(c=>(c.monthly||[]).map(r=>r.date)).sort().at(-1)||'';
        const values=selected.map(c=>(c.monthly||[]).find(r=>r.date===month)?.price).filter(positive);
        return {month,value:values.length?values.reduce((a,b)=>a+b,0)/values.length:null,count:values.length,total:selected.length};
    }
    const ratio=(price,income)=>positive(price)&&positive(income)?price*90/income:null;
    /** A year-only snapshot becomes eligible at year end; a month-only one at month end. */
    function wageCutoff(record){
        const period=record.as_of||'';
        if(/^\d{4}$/.test(period))return period+'-12-31';
        if(/^\d{4}-\d{2}$/.test(period))return period+'-31';
        return /^\d{4}-\d{2}-\d{2}$/.test(period)?period:'9999-12-31';
    }
    function wageSnapshot(records,month){
        if(!/^\d{4}-\d{2}$/.test(month||''))return undefined;
        return (records||[]).filter(r=>wageCutoff(r)<=month+'-31').slice()
            .sort((a,b)=>wageCutoff(b).localeCompare(wageCutoff(a)))[0];
    }
    /** Keep tiers with a published wage at an actual chart month, including retired tiers.
     * @param {Array} records @param {string} region @param {string[]} months
     * @param {Array} currentBands @returns {Array}
     */
    function wageHistoryBands(records,region,months,currentBands=[]){
        const tiers=new Set(currentBands.map(b=>b.tier));
        for(const month of months){
            const row=wageSnapshot(records,month)?.rows.find(r=>r.region===region);
            (row?.monthly||[]).forEach((v,i)=>{if(positive(v))tiers.add(i+1);});
        }
        return [...tiers].sort((a,b)=>a-b).map(tier=>currentBands.find(b=>b.tier===tier)||
            {region,tier,monthly:null,annual:null,ratio:null});
    }
    /** Same-month CIH rental yield; never substitute another city, month or price source. */
    function rentalSnapshot(cities, observations, {city='',month=''}={}) {
        const names=new Set(city?[city]:cities.map(c=>c.city));
        const periods=new Map();
        for(const row of observations||[]) {
            const unit=row.kind==='rent'?'元/㎡·月':row.kind==='second_hand'?'元/㎡':undefined;
            if(!unit||row.unit!==unit||!positive(row.price)||!names.has(row.city)||!/^\d{4}-(0[1-9]|1[0-2])$/.test(row.period))continue;
            if(!periods.has(row.period))periods.set(row.period,new Map());
            const records=periods.get(row.period);
            if(!records.has(row.city))records.set(row.city,{rent:new Set(),second_hand:new Set()});
            records.get(row.city)[row.kind].add(row.price);
        }
        const pairs=period=>[...(periods.get(period)||new Map()).values()]
            .filter(r=>r.rent.size===1&&r.second_hand.size===1)
            .map(r=>({rent:[...r.rent][0],price:[...r.second_hand][0]}));
        const latestMonth=[...periods.keys()].sort().reverse().find(m=>pairs(m).length)||'';
        const rows=pairs(month),count=rows.length;
        const averagePrice=count?rows.reduce((sum,r)=>sum+r.price,0)/count:null;
        const monthlyRent=count?rows.reduce((sum,r)=>sum+r.rent,0)/count:null;
        const monthlyPct=count?monthlyRent/averagePrice*100:null;
        return {month,latestMonth,count,total:names.size,averagePrice,monthlyRent,monthlyPct,
            annualPct:monthlyPct==null?null:monthlyPct*12};
    }
    function calculate(cities,data,{city='',month='',year}={}){
        year=year??Math.max(...(data.income||[]).map(r=>r.year));
        const price=priceSnapshot(cities,city,month),annual=(data.income||[]).find(r=>r.year===Number(year));
        const national=annual?.metrics?.national||{};
        const comparable=!!annual&&annual.year<=Number(price.month.slice(0,4));
        const wage=wageSnapshot(data.minimum_wages,price.month);
        const province=data.city_provinces?.[city];
        const direct=['北京','上海','天津','重庆','深圳'].includes(city)?wage?.rows.find(r=>r.region===city):undefined,regional=wage?.rows.find(r=>r.region===province);
        const rows=city?(direct?[direct]:regional?[regional]:[]):wage?.rows||[];
        const wages=rows.flatMap(r=>r.monthly).filter(positive),min=wages.length?Math.min(...wages):null,max=wages.length?Math.max(...wages):null;
        const wageLabel=!city?'全国已公布月最低工资最低档':direct?city+'月最低工资标准':province?province+'省级分档范围（非本市适用档）':'地区最低工资未收录';
        const cards=[
            {label:'全国人均可支配收入均值',income:national.disposable_mean,ratio:comparable?ratio(price.value,national.disposable_mean):null},
            {label:'全国人均可支配收入中位数',income:national.disposable_median,ratio:comparable?ratio(price.value,national.disposable_median):null},
            {label:'全国城镇人均收入中位数',income:annual?.metrics?.urban?.disposable_median,ratio:comparable?ratio(price.value,annual?.metrics?.urban?.disposable_median):null}
        ];
        const wageBands=rows.flatMap(r=>r.monthly.map((v,i)=>({region:r.region,tier:i+1,monthly:v,annual:positive(v)?v*12:null,ratio:ratio(price.value,positive(v)?v*12:null)}))).filter(r=>positive(r.monthly));
        const surplus=positive(national.disposable_mean)&&positive(national.consumption)?national.disposable_mean-national.consumption:null;
        return {price,annual,national,comparable,cards,cost:positive(price.value)?price.value*90:null,area:90,province,
            wage,wageLabel,wageMin:min,wageMax:max,wageExact:!!direct&&min===max,wageRows:rows,wageBands,
            wageRatioHigh:ratio(price.value,positive(min)?min*12:null),
            wageRatioLow:ratio(price.value,positive(max)?max*12:null),
            surplus,surplusRatio:comparable?ratio(price.value,surplus):null};
    }
    window.HP=window.HP||{};HP.incomeAnalysis={priceSnapshot,ratio,calculate,rentalSnapshot,wageSnapshot,wageHistoryBands};
})();
