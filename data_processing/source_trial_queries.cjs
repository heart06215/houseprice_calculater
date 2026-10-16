'use strict';
// §7.3 nine-group query matrix for the isolated 北京/上海 trial data, computed
// offline through the real engine. Run with cwd = repo root.
// CLI: node data_processing/source_trial_queries.cjs <isolate_root> <out_file> [run_id]
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),crypto=require('node:crypto');
const root=path.resolve(__dirname,'..');
if(!process.argv[2]||!process.argv[3]){console.error('usage: node data_processing/source_trial_queries.cjs <isolate_root> <out_file> [run_id]');process.exit(2);}
const isolate=path.resolve(root,process.argv[2]);
const engineRel='data_processing/frontend/realestate/assets/js/community.js';
const execYear=2026;

function relPosix(p){return path.relative(root,p).replace(/\\/g,'/');}
function sha256File(p){return crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');}
const codeHash=sha256File(path.join(root,engineRel));
const inputHashes={};
for(const rel of ['data/communities/北京.json','data/communities/上海.json','data/metro_operational.min.json']){
    const abs=path.join(isolate,rel);
    if(fs.existsSync(abs))inputHashes[relPosix(abs)]=sha256File(abs);
}
const sortedInputs=Object.fromEntries(Object.keys(inputHashes).sort().map(k=>[k,inputHashes[k]]));
const autoId=crypto.createHash('sha256').update(JSON.stringify({code_hash:codeHash,inputs:sortedInputs})).digest('hex').slice(0,12);
// run_id from CLI wins; otherwise fall back to the code+input short hash, reused as data_version.
const runId=process.argv[4]||autoId;
const dataVersion=runId;

const sandbox={console};sandbox.window=sandbox;
vm.runInNewContext(fs.readFileSync(path.join(root,engineRel),'utf8'),sandbox);
const Q=sandbox.HP.RE.community;

const metro=JSON.parse(fs.readFileSync(path.join(isolate,'data/metro_operational.min.json'),'utf8'));
// Flatten cities -> station objects; engine picks nearest across all cities.
const stations=Object.entries(metro.cities).flatMap(([city,rows])=>rows.map(r=>({city,name:r[1],lng:r[2],lat:r[3],operational_status:r[5],precision:r[6]})));

function project(raw){const cols=raw.cols,rows=raw.rows;return rows.map(v=>Object.fromEntries(cols.map((k,i)=>[k,v[i]])));}

// identity_conflicts.jsonl carries per-name merge decisions; not_merged objects
// are flagged so a 符合 count is not read as a consolidated physical community.
const identitySet=new Set();
const conflictsPath=path.join(isolate,'identity_conflicts.jsonl');
if(fs.existsSync(conflictsPath)){
    for(const line of fs.readFileSync(conflictsPath,'utf8').split(/\r?\n/)){
        if(!line.trim())continue;
        try{const rec=JSON.parse(line);if(rec&&rec.decision==='not_merged'&&rec.name)identitySet.add(rec.name);}catch(_){}
    }
}

// §7.3 exactly nine groups; 1-8 always 含别墅类 + 运营地铁直线 ≤2000 米, 9 resets all.
const GROUPS=[
    {key:'any',group:'不限年份',yearFrom:null,yearTo:null},
    {key:'y1990s',group:'1990-1999',yearFrom:1990,yearTo:1999},
    {key:'y2000-2015',group:'2000-2015',yearFrom:2000,yearTo:2015},
    {key:'y2010-2020',group:'2010-2020',yearFrom:2010,yearTo:2020},
    {key:'y2021-exec',group:'2021-执行年份',yearFrom:2021,yearTo:execYear},
    {key:'start-only',group:'只填起始年',yearFrom:2000,yearTo:null},
    {key:'end-only',group:'只填截止年',yearFrom:null,yearTo:2015},
    // 清空年份 uses the same opts as 不限年份, kept as a separate row so cleared != any is provable.
    {key:'year-cleared',group:'清空年份',yearFrom:null,yearTo:null},
    {key:'all-reset',group:'全部筛选重置',yearFrom:null,yearTo:null,reset:true}
];
const NAME_CAP=200,OBJECT_CAP=80;

function condOf(conds,condition){return conds.find(c=>c.condition===condition)||null;}
function capNames(list){return list.slice(0,NAME_CAP);}

function objectEvidence(p){
    const ev=p.evidence||{},y=ev.year||{},metroEv=ev.metro||{},mv=metroEv.versions||{};
    return {
        name:p.name,
        id:p.id!=null?p.id:null,
        district:p.district||'',
        source:p.source||'',
        year_source:{
            platform:p.source||'',
            source_url:p.source_url||'',
            status:y.status!=null?y.status:null,
            start:y.start!=null?y.start:null,
            end:y.end!=null?y.end:null,
            precision:y.precision!=null?y.precision:null,
            meaning:y.meaning!=null?y.meaning:null,
            scope:y.scope!=null?y.scope:null,
            label:y.label!=null?y.label:null
        },
        form_evidence:Object.assign({},ev.form||{}),
        location_evidence:Object.assign({},ev.location||{},{
            coordinate_system:p.coordinate_system!=null?p.coordinate_system:null,
            precision:p.precision!=null?p.precision:null,
            location_note:p.location_note||''
        }),
        metro_evidence:{
            station:metroEv.station||'',
            meters:metroEv.meters!=null?metroEv.meters:null,
            lower_m:metroEv.lower_m!=null?metroEv.lower_m:null,
            upper_m:metroEv.upper_m!=null?metroEv.upper_m:null,
            caliber:metroEv.caliber||null,
            scope:metroEv.scope||null,
            station_set_version:mv.station_set||null,
            station_id:metroEv.station_id!=null?metroEv.station_id:null
        },
        identity_conflict:identitySet.has(p.name)
    };
}

const result={
    schema:'trial_queries/v1',
    generated_at:new Date().toISOString(),
    city_scope:['北京','上海'],
    isolate_root:relPosix(isolate),
    run_id:runId,
    data_version:dataVersion,
    code_hash:codeHash,
    input_hashes:sortedInputs,
    engine:engineRel,
    versions:metro.versions,
    station_set:metro.versions&&metro.versions.station_set,
    note:'§7.3 九组查询矩阵：北京、上海隔离试验数据经真实前端引擎离线计算；第 1-8 组固定含别墅类 + 运营地铁直线 ≤2000 米，第 9 组全部筛选重置；各组与顶层共用同一 data_version。',
    overlap_note:'各未知原因可重叠，禁止相加为唯一小区总数；符合数为引擎判定，不是独立事实核验；年份范围部分重叠单独计数，清空年份为独立行不与重置合并。',
    cities:{}
};

for(const city of ['北京','上海']){
    const raw=JSON.parse(fs.readFileSync(path.join(isolate,'data/communities',city+'.json'),'utf8'));
    const rows=project(raw);
    result.cities[city]={row_total:rows.length,data_version:dataVersion,groups:[]};

    for(const g of GROUPS){
        const forms=g.reset?[]:['别墅'];
        const metroMaxM=g.reset?null:2000;
        const opts={city,yearFrom:g.yearFrom,yearTo:g.yearTo,forms,metroMaxM,stations,versions:metro.versions,metroScope:'operational'};

        const q=Q.query(rows,opts);
        const counts={
            '符合':q.counts.supported,
            '不符':q.counts.excluded,
            '待核':q.counts.pending,
            '年份范围部分重叠':0,
            '年份未知':0,
            '类型未知':0,
            '位置或地铁未知':0,
            uncovered:q.counts.uncovered,
            total:rows.length
        };
        const partialRows=[];
        rows.forEach(p=>{
            const conds=Q.explain(p,opts);
            const year=condOf(conds,'年份'),form=condOf(conds,'形态'),
                  metroC=condOf(conds,'地铁距离'),loc=condOf(conds,'位置');
            const yearPending=!!year&&year.status==='pending';
            if(yearPending&&String(year.reason||'').indexOf('部分重叠')>=0){counts['年份范围部分重叠']++;partialRows.push(p);}
            else if(yearPending)counts['年份未知']++;
            if(form&&form.status==='pending')counts['类型未知']++;
            if((metroC&&metroC.status==='pending')||(loc&&loc.status==='pending'))counts['位置或地铁未知']++;
        });

        const names={
            '符合':capNames(q.supported.map(p=>p.name)),
            '不符':capNames(q.excluded.map(p=>p.name)),
            '待核':capNames(q.pending.map(p=>p.name)),
            '年份范围部分重叠':capNames(partialRows.map(p=>p.name))
        };
        const namesTruncated={
            '符合':q.supported.length>names['符合'].length,
            '不符':q.excluded.length>names['不符'].length,
            '待核':q.pending.length>names['待核'].length,
            '年份范围部分重叠':partialRows.length>names['年份范围部分重叠'].length
        };

        // Ordered 符合 → 待核 → 年份范围部分重叠 → 不符, distinct rows, capped.
        const ordered=[],seen=new Set();
        for(const list of [q.supported,q.pending,partialRows,q.excluded]){
            for(const p of list){if(!seen.has(p)){seen.add(p);ordered.push(p);}}
        }
        const objects=ordered.slice(0,OBJECT_CAP).map(objectEvidence);

        const pick=q.supported[0]||q.pending[0]||rows[0]||null;
        result.cities[city].groups.push({
            key:g.key,
            group:g.group,
            yearFrom:g.yearFrom,
            yearTo:g.yearTo,
            data_version:dataVersion,
            base:{forms,metroMaxM},
            engine_counts:q.counts,
            checkable:pick?{name:pick.name,source_url:pick.source_url||''}:null,
            counts,
            names,
            names_truncated:namesTruncated,
            objects,
            objects_truncated:ordered.length>OBJECT_CAP
        });
    }
}

const out=path.resolve(root,process.argv[3]);
fs.mkdirSync(path.dirname(out),{recursive:true});
fs.writeFileSync(out,JSON.stringify(result,null,2)+'\n');
console.log('[trial_queries] cities='+result.city_scope.length+' groups='+GROUPS.length+' run_id='+runId+' -> '+path.relative(root,out));
