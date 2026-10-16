// Exercise the actual layer with deterministic elevation pixels, including zoom > DEM limit.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync(__dirname+'/frontend/realestate/assets/js/contours.js','utf8');
async function run(zoom,fail=false){
  let definition,paths=[],requests=[],position,statusNode,strokeCount=0;
  const context={clearRect(){},save(){},restore(){},setTransform(){},beginPath(){},stroke(){strokeCount++},
    moveTo(x,y){paths.push([x,y])},lineTo(x,y){paths.push([x,y])},drawImage(){},
    getImageData(){let data=new Uint8ClampedArray(256*256*4);for(let y=0;y<256;y++)for(let x=0;x<256;x++){
      let elevation=32768+100+x+y,o=(y*256+x)*4;data[o]=Math.floor(elevation/256);data[o+1]=elevation%256;data[o+3]=255;
    }return {data}}};
  const node=()=>({width:256,height:256,style:{},isConnected:true,setAttribute(){},getContext:()=>context});
  const pane={insertBefore(c){c.parentNode=pane},removeChild(c){c.isConnected=false}};
  const map={getSize:()=>({x:160,y:120}),getZoom:()=>zoom,getPanes:()=>({overlayPane:pane}),
    containerPointToLayerPoint:()=>({x:-37,y:19}),containerPointToLatLng:()=>({}),
    project:(_,z)=>({x:(2**z/2)*256+20,y:(2**z/2)*256+20}),on(){},off(){},removeLayer(l){l.onRemove(this)}};
  const sandbox={window:{},Float32Array,Promise,Math,Number,Infinity,isNaN,isFinite,setTimeout,clearTimeout,
    document:{createElement:()=>node()},Image:class{constructor(){this.width=256;this.height=256}set src(url){requests.push(url);queueMicrotask(()=>fail?this.onerror():this.onload())}},
    L:{DomUtil:{create(tag){let n=node();if(tag==='div')statusNode=n;return n},setPosition(c,p){position=p}},
      control:()=>({addTo(){this.onAdd()},remove(){}}),Layer:{extend(d){definition=d;return class{constructor(){Object.assign(this,d)}addTo(m){this.onAdd(m)}}}}}};
  sandbox.HP=sandbox.window.HP={};vm.runInNewContext(code,sandbox);
  sandbox.HP.RE.contours.setEnabled(true,map);
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepStrictEqual(position,{x:-37,y:19});
  for(const url of requests){const match=url.match(/terrarium\/(\d+)\/(\d+)\/(\d+)/);assert(match);const n=2**Number(match[1]);assert(+match[2]<n&&+match[3]<n,'tile coordinates must match DEM zoom')}
  if(fail)assert.match(statusNode.textContent,/加载失败/);
  else{assert(strokeCount>0,'sloping synthetic terrain must produce contours');assert(paths.every(p=>p.every(Number.isFinite)),'crossings cannot extrapolate to NaN');assert(paths.every(([x,y])=>x>=0&&y>=0&&x<170&&y<130));assert.match(statusNode.textContent,/等高距/)}
  sandbox.HP.RE.contours.setEnabled(false,map);
}
(async()=>{for(const z of [10,11,16])await run(z);await run(11,true);console.log('Contour regression tests passed: tile zoom, crossings, positioning, failure feedback');})().catch(e=>{console.error(e);process.exitCode=1});
