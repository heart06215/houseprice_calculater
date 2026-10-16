/* 数据加载：内联数据（standalone/file:// 模式）优先，HTTP 模式走 fetch。 */
window.HP = window.HP || {};

HP.loadData = function () {
    if (window.__HOUSING_DATA__) {
        return Promise.resolve(window.__HOUSING_DATA__);
    }
    return fetch('intersection_housing_data.json?v=20260918-water15', {cache:'no-cache'}).then(function (r) {
        if (!r.ok) throw new Error('数据文件加载失败: HTTP ' + r.status);
        return r.json();
    });
};
