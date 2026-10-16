/*! Motion mini 13.4.1; bundled locally.
motion 13.4.1
The MIT License (MIT)

Copyright (c) 2024 [Motion](https://motion.dev) B.V.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


framer-motion 13.4.1
The MIT License (MIT)

Copyright (c) 2018 Framer B.V.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


motion-dom 13.4.1
The MIT License (MIT)

Copyright (c) 2024 [Motion](https://motion.dev) B.V.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


motion-utils 13.3.0
The MIT License (MIT)

Copyright (c) 2024 [Motion](https://motion.dev) B.V.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


tslib 2.8.1
Copyright (c) Microsoft Corporation.

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
*/
var Motion=(()=>{var N=Object.defineProperty;var he=Object.getOwnPropertyDescriptor;var ge=Object.getOwnPropertyNames;var ye=Object.prototype.hasOwnProperty;var Te=(t,e)=>{for(var r in e)N(t,r,{get:e[r],enumerable:!0})},Se=(t,e,r,o)=>{if(e&&typeof e=="object"||typeof e=="function")for(let i of ge(e))!ye.call(t,i)&&i!==r&&N(t,i,{get:()=>e[i],enumerable:!(o=he(e,i))||o.enumerable});return t};var Ve=t=>Se(N({},"__esModule",{value:!0}),t);var Pe={};Te(Pe,{animateMini:()=>U});var S=()=>{};var V={};function k(t){let e;return()=>(e===void 0&&(e=t()),e)}var A=t=>t;var g=t=>t*1e3,E=t=>t/1e3;var G=t=>Array.isArray(t)&&typeof t[0]=="number";var v=["setup","read","resolveKeyframes","preUpdate","update","preRender","render","postRender"];function Z(t){let e=new Set,r=new Set,o=!1,i=!1,s=new Set,p={delta:0,timestamp:0,isProcessing:!1};function c(n){s.has(n)&&(r.add(n),t()),n(p)}let m={schedule:(n,a=!1,l=!1)=>{let u=l&&o?e:r;return a&&s.add(n),u.add(n),n},cancel:n=>{r.delete(n),s.delete(n)},process:n=>{if(p=n,o){i=!0;return}o=!0;let a=e;e=r,r=a,e.forEach(c),e.clear(),o=!1,i&&(i=!1,m.process(n))}};return m}var Ae=40;function J(t,e){let r=!1,o=!0,i={delta:0,timestamp:0,isProcessing:!1},s=()=>r=!0,p=v.reduce((x,d)=>(x[d]=Z(s),x),{}),{setup:c,read:m,resolveKeyframes:n,preUpdate:a,update:l,preRender:f,render:u,postRender:P}=p,T=()=>{let x=V.useManualTiming,d=x?i.timestamp:performance.now();r=!1,x||(i.delta=o?1e3/60:Math.max(Math.min(d-i.timestamp,Ae),1)),i.timestamp=d,i.isProcessing=!0,c.process(i),m.process(i),n.process(i),a.process(i),l.process(i),f.process(i),u.process(i),P.process(i),i.isProcessing=!1,r&&e&&(o=!1,t(T))},D=()=>{r=!0,o=!0,i.isProcessing||t(T)};return{schedule:v.reduce((x,d)=>{let ue=p[d];return x[d]=(ce,de=!1,xe=!1)=>(r||D(),ue.schedule(ce,de,xe)),x},{}),cancel:x=>{for(let d=0;d<v.length;d++)p[v[d]].cancel(x)},state:i,steps:p}}var{schedule:Qe,cancel:et,state:y,steps:tt}=J(typeof requestAnimationFrame<"u"?requestAnimationFrame:A,!0);var B;function Ee(){B=void 0}var L={now:()=>(B===void 0&&L.set(y.isProcessing||V.useManualTiming?y.timestamp:performance.now()),B),set:t=>{B=t,queueMicrotask(Ee)}};var X=(t,e,r=10)=>{let o="",i=Math.max(Math.round(e/r),2);for(let s=0;s<i;s++)o+=Math.round(t(s/(i-1))*1e4)/1e4+", ";return`linear(${o.substring(0,o.length-2)})`};var ve=t=>t!==null;function Y(t,{repeat:e,repeatType:r="loop"},o,i=1){let s=t.filter(ve),c=i<0||e&&r!=="loop"&&e%2===1?0:s.length-1;return!c||o===void 0?s[c]:o}function be(t,e){return{kind:t,animation:e,timestamp:L.now(),frameTimestamp:y.timestamp,frameIsProcessing:y.isProcessing}}function Q(t,e,r){let o=globalThis.__MOTION_INSPECT__;if(o)try{o({...be("animation-start",t),options:r?{...e,...r}:e})}catch{}}var w=class{constructor(){this.isResolved=!1}get finished(){return this._finished||(this._finished=this.isResolved?Promise.resolve():new Promise(e=>{this._resolve=e})),this._finished}updateFinished(){this._finished=this._resolve=void 0,this.isResolved=!1}notifyFinished(){this.isResolved=!0,this._resolve?.()}then(e,r){return this.finished.then(e,r)}};function W(t){for(let e=1;e<t.length;e++)t[e]??(t[e]=t[e-1])}var F=t=>t.startsWith("--");function ee(t,e,r){F(e)?t.style.setProperty(e,r):t.style[e]=r}var te={};function R(t,e){let r=k(t);return()=>te[e]??r()}var re=R(()=>window.ScrollTimeline!==void 0,"scrollTimeline");var C=R(()=>{try{document.createElement("div").animate({opacity:0},{easing:"linear(0, 1)"})}catch{return!1}return!0},"linearEasing");var h=([t,e,r,o])=>`cubic-bezier(${t}, ${e}, ${r}, ${o})`;var K={linear:"linear",ease:"ease",easeIn:"ease-in",easeOut:"ease-out",easeInOut:"ease-in-out",circIn:h([0,.65,.55,1]),circOut:h([.55,0,1,.45]),backIn:h([.31,.01,.66,-.59]),backOut:h([.33,1.53,.69,.99])};function _(t,e){if(t)return typeof t=="function"?C()?X(t,e):"ease-out":G(t)?h(t):Array.isArray(t)?t.map(r=>_(r,e)||K.easeOut):K[t]}function oe(t,e,r,{delay:o=0,duration:i=300,repeat:s=0,repeatType:p="loop",ease:c="easeOut",times:m}={},n=void 0){let a={[e]:r};m&&(a.offset=m);let l=_(c,i);Array.isArray(l)&&(a.easing=l);let f={delay:o,duration:i,easing:Array.isArray(l)?"linear":l,fill:"both",iterations:s+1,direction:p==="reverse"?"alternate":"normal"};return n&&(f.pseudoElement=n),t.animate(a,f)}function ie(t){return typeof t=="function"&&"applyToOptions"in t}function ne({type:t,...e}){return ie(t)&&C()?t.applyToOptions(e):(e.duration??(e.duration=300),e.ease??(e.ease="easeOut"),e)}var b=class extends w{constructor(e){if(super(),this.finishedTime=null,this.isStopped=!1,this.manualStartTime=null,!e)return;let{element:r,name:o,keyframes:i,pseudoElement:s,allowFlatten:p=!1,finalKeyframe:c,onComplete:m}=e;this.isPseudoElement=!!s,this.allowFlatten=p,this.options=e,S(typeof e.type!="string",`Mini animate() doesn't support "type" as a string.`,"mini-spring");let n=ne(e);this.animation=oe(r,o,i,n,s),n.autoplay===!1&&this.animation.pause(),this.animation.onfinish=()=>{if(this.finishedTime=this.time,!s){let a=Y(i,this.options,c,this.speed);this.updateMotionValue&&this.updateMotionValue(a),ee(r,o,a),this.animation.cancel()}m?.(),this.notifyFinished()},Q(this,e,n)}play(){this.isStopped||(this.manualStartTime=null,this.animation.play(),this.state==="finished"&&this.updateFinished())}pause(){this.animation.pause()}complete(){this.animation.finish?.()}cancel(){try{this.animation.cancel()}catch{}}stop(){if(this.isStopped)return;this.isStopped=!0;let{state:e}=this;e==="idle"||e==="finished"||(this.updateMotionValue?this.updateMotionValue():this.commitStyles(),this.isPseudoElement||this.cancel())}commitStyles(){let e=this.options?.element;!this.isPseudoElement&&e?.isConnected&&this.animation.commitStyles?.()}get duration(){let e=this.animation.effect?.getComputedTiming?.().duration||0;return E(Number(e))}get iterationDuration(){let{delay:e=0}=this.options||{};return this.duration+E(e)}get time(){return E(Number(this.animation.currentTime)||0)}set time(e){let r=this.finishedTime!==null;this.manualStartTime=null,this.finishedTime=null,this.animation.currentTime=g(e),r&&this.animation.pause()}get speed(){return this.animation.playbackRate}set speed(e){e<0&&(this.finishedTime=null),this.animation.playbackRate=e}get state(){return this.finishedTime!==null?"finished":this.animation.playState}get startTime(){return this.manualStartTime??Number(this.animation.startTime)}set startTime(e){this.manualStartTime=this.animation.startTime=e}attachTimeline({timeline:e,rangeStart:r,rangeEnd:o,observe:i}){return this.allowFlatten&&this.animation.effect?.updateTiming({easing:"linear"}),this.animation.onfinish=null,e&&re()?(this.animation.timeline=e,r&&(this.animation.rangeStart=r),o&&(this.animation.rangeEnd=o),A):i(this)}};var I=class{constructor(e){this.stop=()=>this.runAll("stop"),this.animations=e.filter(Boolean)}get finished(){return Promise.all(this.animations.map(e=>e.finished))}getAll(e){return this.animations[0][e]}setAll(e,r){for(let o=0;o<this.animations.length;o++)this.animations[o][e]=r}attachTimeline(e){let r=this.animations.map(o=>o.attachTimeline(e));return()=>{r.forEach((o,i)=>{o&&o(),this.animations[i].stop()})}}get time(){return this.getAll("time")}set time(e){this.setAll("time",e)}get speed(){return this.getAll("speed")}set speed(e){this.setAll("speed",e)}get state(){return this.getAll("state")}get startTime(){return this.getAll("startTime")}get duration(){return ae(this.animations,"duration")}get iterationDuration(){return ae(this.animations,"iterationDuration")}runAll(e){this.animations.forEach(r=>r[e]())}play(){this.runAll("play")}pause(){this.runAll("pause")}cancel(){this.runAll("cancel")}complete(){this.runAll("complete")}};function ae(t,e){let r=0;for(let o=0;o<t.length;o++){let i=t[o][e];i!==null&&i>r&&(r=i)}return r}var M=class extends I{then(e,r){return this.finished.finally(e).then(()=>{})}};var se=new WeakMap,z=(t,e="")=>`${t}:${e}`;function j(t){let e=se.get(t);return e||(e=new Map,se.set(t,e)),e}function me(t,e){if(t?.inherit&&e){let{inherit:r,...o}=t;return{...e,...o}}return t}function q(t,e){let r=t?.[e]??t?.default??t;return r!==t?me(r,t):r}var pe=["borderTopLeftRadius","borderTopRightRadius","borderBottomRightRadius","borderBottomLeftRadius"];var le=new Set(["borderWidth","borderTopWidth","borderRightWidth","borderBottomWidth","borderLeftWidth","borderRadius",...pe,"width","maxWidth","height","maxHeight","top","right","bottom","left","inset","insetBlock","insetBlockStart","insetBlockEnd","insetInline","insetInlineStart","insetInlineEnd","padding","paddingTop","paddingRight","paddingBottom","paddingLeft","paddingBlock","paddingBlockStart","paddingBlockEnd","paddingInline","paddingInlineStart","paddingInlineEnd","margin","marginTop","marginRight","marginBottom","marginLeft","marginBlock","marginBlockStart","marginBlockEnd","marginInline","marginInlineStart","marginInlineEnd","fontSize","backgroundPositionX","backgroundPositionY"]);function H(t,e){for(let r=0;r<t.length;r++)typeof t[r]=="number"&&le.has(e)&&(t[r]=t[r]+"px")}function $(t,e,r){if(t==null)return[];if(t instanceof EventTarget)return[t];if(typeof t=="string"){let o=document;e&&(o=e.current);let i=r?.[t]??o.querySelectorAll(t);return i?Array.from(i):[]}return Array.from(t).filter(o=>o!=null)}function O(t,e){let r=window.getComputedStyle(t);return F(e)?r.getPropertyValue(e):r[e]}function fe(t,e,r,o){if(t==null)return[];let i=$(t,o),s=i.length;S(!!s,"No valid elements provided.","no-valid-elements");let p=[];for(let m=0;m<s;m++){let n=i[m],a={...r};typeof a.delay=="function"&&(a.delay=a.delay(m,s));for(let l in e){let f=e[l];Array.isArray(f)||(f=[f]);let u={...q(a,l)};u.duration&&(u.duration=g(u.duration)),u.delay&&(u.delay=g(u.delay));let P=j(n),T=z(l,u.pseudoElement||""),D=P.get(T);D&&D.stop(),p.push({map:P,key:T,unresolvedKeyframes:f,options:{...u,element:n,name:l,allowFlatten:!a.type&&!a.ease}})}}for(let m=0;m<p.length;m++){let{unresolvedKeyframes:n,options:a}=p[m],{element:l,name:f,pseudoElement:u}=a;!u&&n[0]===null&&(n[0]=O(l,f)),W(n),H(n,f),!u&&n.length<2&&n.unshift(O(l,f)),a.keyframes=n}let c=[];for(let m=0;m<p.length;m++){let{map:n,key:a,options:l}=p[m],f=new b(l);n.set(a,f),f.finished.finally(()=>n.delete(a)),c.push(f)}return c}var Me=t=>{function e(r,o,i){return new M(fe(r,o,i,t))}return e},U=Me();return Ve(Pe);})();
