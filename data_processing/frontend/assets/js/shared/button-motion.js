/* Delegated Motion feedback also covers Vue and map controls created later. */
(function () {
    'use strict';
    if (!window.Motion || !Motion.animateMini) return;
    const selector = 'button, .community-actions a, .n-tip .btn';
    const reduced = matchMedia('(prefers-reduced-motion: reduce)');
    const fine = matchMedia('(hover: hover) and (pointer: fine)');
    const states = new Map();
    const pointers = new Map();
    const listeners = new AbortController();
    const options = {signal: listeners.signal};

    function target(event) {
        const el = event.target instanceof Element ? event.target.closest(selector) : null;
        return el && !el.matches(':disabled, .disabled, [aria-disabled="true"], [aria-busy="true"]') ? el : null;
    }

    function reset(el) {
        const state = states.get(el);
        if (!state) return;
        if (state.animation) state.animation.stop();
        el.style.transform = state.original;
        states.delete(el);
    }

    function resetAll() {
        for (const el of states.keys()) reset(el);
        pointers.clear();
    }

    function update(el, changes) {
        if (!el) return;
        if (reduced.matches || !el.isConnected || el.matches(':disabled, [aria-disabled="true"]')) {
            reset(el);
            return;
        }
        let state = states.get(el);
        if (!state) {
            state = {hover: false, pressed: false, original: el.style.transform, animation: null};
            states.set(el, state);
        }
        Object.assign(state, changes);
        if (state.animation) state.animation.stop();
        const to = state.pressed ? 'translateY(1px) scale(0.985)' : 'translateY(0px) scale(1)'; /* hover lift removed 2026-10-11 */
        const animation = Motion.animateMini(el, {transform: [getComputedStyle(el).transform, to]}, {
            duration: state.pressed ? 0.1 : 0.16, ease: [0.22, 0.7, 0.24, 1]
        });
        state.animation = animation;
        animation.then(function () {
            if (states.get(el)?.animation === animation && !state.hover && !state.pressed) reset(el);
        });
    }

    document.documentElement.classList.add('hp-motion-ready');
    document.addEventListener('pointerover', function (event) {
        const el = target(event);
        if (fine.matches && event.pointerType !== 'touch' && el && !el.contains(event.relatedTarget)) update(el, {hover: true});
    }, options);
    document.addEventListener('pointerout', function (event) {
        const el = event.target instanceof Element ? event.target.closest(selector) : null;
        if (el && states.has(el) && !el.contains(event.relatedTarget)) update(el, {hover: false});
    }, options);
    document.addEventListener('pointerdown', function (event) {
        const el = target(event);
        if (event.button !== 0 || !el) return;
        pointers.set(event.pointerId, el);
        update(el, {pressed: true});
    }, options);
    function release(event) {
        const el = pointers.get(event.pointerId);
        pointers.delete(event.pointerId);
        update(el, event.type === 'pointercancel' ? {pressed: false, hover: false} : {pressed: false});
    }
    document.addEventListener('pointerup', release, options);
    document.addEventListener('pointercancel', release, options);
    document.addEventListener('keydown', function (event) {
        if (!event.repeat && (event.key === 'Enter' || event.key === ' ')) update(target(event), {pressed: true});
    }, options);
    document.addEventListener('keyup', function (event) {
        if (event.key === 'Enter' || event.key === ' ') update(target(event), {pressed: false});
    }, options);
    document.addEventListener('focusout', function (event) {
        const el = event.target instanceof Element ? event.target.closest(selector) : null;
        if (states.has(el)) update(el, {pressed: false});
    }, options);
    window.addEventListener('blur', resetAll, options);
    window.addEventListener('pagehide', resetAll, options);
    reduced.addEventListener('change', resetAll, options);
    fine.addEventListener('change', resetAll, options);
    const observer = new MutationObserver(function () {
        for (const el of states.keys()) {
            if (!el.isConnected || el.matches(':disabled, [aria-disabled="true"], [aria-busy="true"]')) reset(el);
        }
    });
    observer.observe(document.documentElement, {childList: true, subtree: true, attributes: true, attributeFilter: ['disabled', 'aria-disabled', 'aria-busy']});
    window.HP = window.HP || {};
    HP.buttonMotion = {destroy: function () {
        resetAll();
        observer.disconnect();
        listeners.abort();
        document.documentElement.classList.remove('hp-motion-ready');
    }};
})();
