/* Amounts in yuan; fixed annual rates in percent. Display rounding never feeds the balance. */
(function(root){
    function schedule(principal, annualRate, years, method) {
        if (![principal,annualRate,years].every(Number.isFinite) || principal < 0 || principal > 1e10 || annualRate < 0 || annualRate > 30 || !Number.isInteger(years) || years < 1 || years > 40 || !['annuity','principal'].includes(method)) throw Error('请填写有效金额、0–30% 年利率及 1–40 年整数期限');
        const n=years*12, r=annualRate/1200;
        const payment=r ? principal*r/-Math.expm1(-n*Math.log1p(r)) : principal/n;
        let balance=principal;
        return Array.from({length:n},(_,i)=>{
            const interest=balance*r;
            const capital=i===n-1 ? balance : Math.min(balance,method==='principal'?principal/n:payment-interest);
            balance=Math.max(0,balance-capital);
            return {month:i+1,principal:capital,interest,payment:capital+interest,balance};
        });
    }
    function calculate(loans,method){
        if(!loans.length || !loans.some(l=>l.principal>0)) throw Error('贷款总额须大于 0');
        const parts=loans.map(l=>schedule(l.principal,l.rate,l.years,method));
        const rows=Array.from({length:Math.max(...parts.map(p=>p.length))},(_,i)=>parts.reduce((s,p)=>{
            for(const k of ['principal','interest','payment','balance'])s[k]+=p[i]?.[k]||0;
            return s;
        },{month:i+1,principal:0,interest:0,payment:0,balance:0}));
        const principal=loans.reduce((s,l)=>s+l.principal,0), interest=rows.reduce((s,r)=>s+r.interest,0);
        return {rows,principal,interest,total:principal+interest,first:rows[0].payment,last:rows.at(-1).payment};
    }
    // Salary is take-home, AFTER the employee contribution; never deduct it a second time.
    const number=(value,label,max=1e8)=>{if(typeof value!=='number'||!Number.isFinite(value)||value<0||value>max)throw Error('请填写有效的'+label);return value;};
    function contributions(input){
        let employee,employer;
        if(input.mode==='ratio'){
            const gross=number(input.grossSalary,'税前月工资');
            const base=input.useCustomBase?number(input.base,'实际公积金缴存基数'):gross;
            employee=base*number(input.employeeRate,'个人缴存比例（0–100%）',100)/100;
            employer=base*number(input.employerRate,'公司缴存比例（0–100%）',100)/100;
        }else if(input.mode==='amount'){
            employee=number(input.employee,'个人月缴存额');employer=number(input.employer,'公司月缴存额');
        }else throw Error('请选择缴存输入方式');
        return {employee,employer,contribution:employee+employer};
    }
    function budget(rows, input){
        const salary=input.salary===''||input.salary==null?null:number(input.salary,'税后到手工资');
        const {employee,employer,contribution}=contributions(input);
        const monthly=rows.map(r=>{
            const fundUsed=Math.min(contribution,r.payment),cashPayment=r.payment-fundUsed;
            return {month:r.month,fundUsed,cashPayment,remaining:salary===null?null:salary-cashPayment,fundRetained:contribution-fundUsed};
        });
        return {employee,employer,contribution,rows:monthly,first:monthly[0],last:monthly.at(-1)};
    }
    const api={schedule,calculate,contributions,budget};
    if(typeof module!=='undefined')module.exports=api;
    else {root.HP=root.HP||{};root.HP.mortgage=api;}
})(typeof window==='undefined'?globalThis:window);
