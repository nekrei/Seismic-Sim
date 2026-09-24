// Spec 19: presentation-only workspace controller. Owns which panel,
// destination and experiment is visible, and the one shared dialog. It never
// touches simulation inputs, never fetches, and imports nothing from the
// viewer: the viewer passes callbacks in.

const STORE_KEY = 'seismic-sim.workspace.v1';
const PHONE = matchMedia('(max-width: 767.98px)');
const COMPACT = matchMedia('(max-width: 1279.98px)');
const REDUCED = matchMedia('(prefers-reduced-motion: reduce)');

function readStore() {
    try { return JSON.parse(sessionStorage.getItem(STORE_KEY)) || {}; } catch { return {}; }
}
function writeStore(state) {
    try { sessionStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch { /* storage unavailable: layout still works */ }
}
const layoutMode = () => (PHONE.matches ? 'phone' : COMPACT.matches ? 'compact' : 'wide');

export function initWorkspace({ onSignalsVisibility, onSignalsSection, onExperiment, onLayoutChange }) {
    const root = document.getElementById('workspace');
    const setupPanel = document.getElementById('controls');
    const signalsPanel = document.getElementById('analysisDrawer');
    const setupToggle = document.getElementById('setupToggleBtn');
    const signalsToggle = document.getElementById('analysisToggleBtn');
    const mobileButtons = [...document.querySelectorAll('[data-mobile]')];
    const setupTabs = [...document.querySelectorAll('[data-setup]')];
    const signalsTabs = [...document.querySelectorAll('[data-signals]')];
    const experimentTabs = [...document.querySelectorAll('[data-experiment]')];
    const scene = document.getElementById('sceneHost');

    const saved = readStore();
    const state = {
        setup: ['building', 'earthquake', 'analysis', 'view'].includes(saved.setup) ? saved.setup : 'building',
        signals: saved.signals === 'experiments' ? 'experiments' : 'charts',
        experiment: ['superposition', 'sweep', 'comparison'].includes(saved.experiment) ? saved.experiment : 'superposition',
        setupOpen: saved.setupOpen ?? true,
        signalsOpen: saved.signalsOpen ?? false,
        mobileView: ['scene', 'setup', 'signals'].includes(saved.mobileView) ? saved.mobileView : 'scene',
        lastOpened: saved.lastOpened || 'setup',
    };
    let mode = layoutMode();
    // Compact shows one panel at a time; keep the most recently opened one.
    if (mode === 'compact' && state.setupOpen && state.signalsOpen) {
        if (state.lastOpened === 'signals') state.setupOpen = false; else state.signalsOpen = false;
    }
    const scroll = {};   // per-page scrollTop, restored when a page returns
    let signalsVisible = null;

    const visible = () => mode === 'phone'
        ? { setup: state.mobileView === 'setup', signals: state.mobileView === 'signals' }
        : { setup: state.setupOpen, signals: state.signalsOpen };

    function persist() {
        writeStore({ setup: state.setup, signals: state.signals, experiment: state.experiment,
            setupOpen: state.setupOpen, signalsOpen: state.signalsOpen, mobileView: state.mobileView,
            lastOpened: state.lastOpened });
    }

    let layoutTimer = 0;
    function scheduleLayout() {
        // Once now (the scene track starts moving) and once after the panel
        // transition settles; the viewer's own ResizeObserver covers frames
        // in between.
        requestAnimationFrame(() => onLayoutChange?.());
        clearTimeout(layoutTimer);
        layoutTimer = setTimeout(() => onLayoutChange?.(), REDUCED.matches ? 0 : 220);
    }

    function apply({ focusFrom } = {}) {
        const v = visible();
        root.dataset.layout = mode;
        root.classList.toggle('setup-visible', v.setup);
        root.classList.toggle('signals-visible', v.signals);
        setupPanel.inert = !v.setup;
        signalsPanel.inert = !v.signals;
        setupToggle.setAttribute('aria-expanded', String(v.setup));
        signalsToggle.setAttribute('aria-expanded', String(v.signals));
        mobileButtons.forEach(b => b.setAttribute('aria-pressed', String(b.dataset.mobile === (mode === 'phone' ? state.mobileView : ''))));
        // Focus never stays inside a panel that just became inert.
        if (focusFrom && focusFrom.contains(document.activeElement) && focusFrom.inert) {
            const target = mode === 'phone'
                ? mobileButtons.find(b => b.dataset.mobile === state.mobileView) || mobileButtons[0]
                : focusFrom === setupPanel ? setupToggle : signalsToggle;
            target.focus();
        }
        if (signalsVisible !== v.signals) {
            signalsVisible = v.signals;
            onSignalsVisibility?.(v.signals);
        }
        persist();
        scheduleLayout();
    }

    function remember(panel) {
        const page = panel.querySelector(panel === setupPanel ? '.setup-page:not([hidden])' : '.signals-section:not([hidden])');
        if (page) scroll[page.id] = page.scrollTop;
    }
    function restore(panel) {
        const page = panel.querySelector(panel === setupPanel ? '.setup-page:not([hidden])' : '.signals-section:not([hidden])');
        if (page && scroll[page.id] != null) page.scrollTop = scroll[page.id];
    }

    function setSetupOpen(open) {
        if (mode === 'phone') {
            if (open) remember(signalsPanel); else remember(setupPanel);
            state.mobileView = open ? 'setup' : 'scene';
        } else {
            if (!open) remember(setupPanel);
            state.setupOpen = open;
            if (open && mode === 'compact' && state.signalsOpen) { remember(signalsPanel); state.signalsOpen = false; }
        }
        if (open) state.lastOpened = 'setup';
        apply({ focusFrom: open ? signalsPanel : setupPanel });
        if (open) restore(setupPanel);
    }
    function setSignalsOpen(open) {
        if (mode === 'phone') {
            if (open) remember(setupPanel); else remember(signalsPanel);
            state.mobileView = open ? 'signals' : 'scene';
        } else {
            if (!open) remember(signalsPanel);
            state.signalsOpen = open;
            if (open && mode === 'compact' && state.setupOpen) { remember(setupPanel); state.setupOpen = false; }
        }
        if (open) state.lastOpened = 'signals';
        apply({ focusFrom: open ? setupPanel : signalsPanel });
        if (open) restore(signalsPanel);
    }

    function showSetup(id, { open = true } = {}) {
        if (id !== state.setup) {
            remember(setupPanel);
            state.setup = id;
        }
        setupTabs.forEach(b => {
            const on = b.dataset.setup === id;
            b.setAttribute('aria-selected', String(on));
            b.tabIndex = on ? 0 : -1;
        });
        setupPanel.querySelectorAll('.setup-page').forEach(p => { p.hidden = p.dataset.page !== id; });
        restore(setupPanel);
        if (open && !visible().setup) setSetupOpen(true); else persist();
    }
    function showSignals(section, { open = true } = {}) {
        if (section !== state.signals) remember(signalsPanel);
        state.signals = section;
        signalsTabs.forEach(b => b.setAttribute('aria-selected', String(b.dataset.signals === section)));
        document.getElementById('signalsCharts').hidden = section !== 'charts';
        document.getElementById('signalsExperiments').hidden = section !== 'experiments';
        restore(signalsPanel);
        onSignalsSection?.(section);
        if (open && !visible().signals) setSignalsOpen(true); else { persist(); scheduleLayout(); }
    }
    function showExperiment(id, { open = true, reveal = true } = {}) {
        state.experiment = id;
        experimentTabs.forEach(b => b.setAttribute('aria-selected', String(b.dataset.experiment === id)));
        document.querySelectorAll('[data-experiment-pane]').forEach(p => { p.hidden = p.dataset.experimentPane !== id; });
        onExperiment?.(id);
        if (reveal) showSignals('experiments', { open }); else persist();
    }

    // Arrow-key roving between tabs of one tablist (WAI-ARIA tabs pattern).
    function arrowNav(buttons, activate) {
        buttons.forEach((b, i) => b.addEventListener('keydown', e => {
            const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
            if (!step) return;
            e.preventDefault();
            const next = buttons[(i + step + buttons.length) % buttons.length];
            next.focus();
            activate(next);
        }));
    }

    setupToggle.addEventListener('click', () => setSetupOpen(!visible().setup));
    document.getElementById('setupHideBtn').addEventListener('click', () => setSetupOpen(false));
    signalsToggle.addEventListener('click', () => setSignalsOpen(!visible().signals));
    document.getElementById('analysisCloseBtn').addEventListener('click', () => setSignalsOpen(false));
    setupTabs.forEach(b => b.addEventListener('click', () => showSetup(b.dataset.setup)));
    arrowNav(setupTabs, b => showSetup(b.dataset.setup));
    signalsTabs.forEach(b => b.addEventListener('click', () => showSignals(b.dataset.signals)));
    arrowNav(signalsTabs, b => showSignals(b.dataset.signals));
    experimentTabs.forEach(b => b.addEventListener('click', () => showExperiment(b.dataset.experiment)));
    arrowNav(experimentTabs, b => showExperiment(b.dataset.experiment));
    mobileButtons.forEach(b => b.addEventListener('click', () => {
        const target = b.dataset.mobile;
        if (target === 'setup') setSetupOpen(true);
        else if (target === 'signals') setSignalsOpen(true);
        else { remember(state.mobileView === 'setup' ? setupPanel : signalsPanel); state.mobileView = 'scene'; apply(); }
    }));
    document.querySelectorAll('[data-goto]').forEach(b => b.addEventListener('click', () => showSetup(b.dataset.goto)));

    // Only a real breakpoint crossing changes the layout; ordinary resizes,
    // orientation changes and the phone keyboard keep the current sheet.
    function onBreakpoint() {
        const next = layoutMode();
        if (next === mode) return;
        const was = visible();
        if (next === 'phone') {
            state.mobileView = was.signals && (state.lastOpened === 'signals' || !was.setup) ? 'signals'
                : was.setup ? 'setup' : 'scene';
        } else if (mode === 'phone') {
            state.setupOpen = state.mobileView === 'setup' || (state.mobileView !== 'signals' && state.setupOpen && next === 'wide');
            state.signalsOpen = state.mobileView === 'signals';
        }
        if (next === 'compact' && state.setupOpen && state.signalsOpen) {
            if (state.lastOpened === 'signals') state.setupOpen = false; else state.signalsOpen = false;
        }
        mode = next;
        apply();
    }
    PHONE.addEventListener('change', onBreakpoint);
    COMPACT.addEventListener('change', onBreakpoint);

    // Initial render: the saved destinations, without opening anything the
    // user had closed.
    showSetup(state.setup, { open: false });
    showExperiment(state.experiment, { open: false, reveal: false });
    showSignals(state.signals, { open: false });
    apply();
    new ResizeObserver(() => onLayoutChange?.()).observe(scene);

    return {
        get state() { return { ...state, mode, visible: visible() }; },
        setSetupOpen, setSignalsOpen, showSetup, showSignals, showExperiment,
    };
}

// ------------------------------------------------------------------ dialog
// One native modal <dialog> for help and result details. showModal() makes
// the page inert and traps Tab; Escape closes (cancel event). Opening a new
// topic replaces the content: dialogs are never stacked. Focus returns to the
// element that opened it.
export function initDialog() {
    const dialog = document.getElementById('infoDialog');
    const title = document.getElementById('infoDialogTitle');
    const body = document.getElementById('infoDialogBody');
    let returnFocus = null;
    let cleanup = null;

    function close() { if (dialog.open) dialog.close(); }
    dialog.addEventListener('close', () => {
        cleanup?.(); cleanup = null;
        body.replaceChildren();
        const target = returnFocus;
        returnFocus = null;
        if (target && target.isConnected && !target.closest('[inert]')) target.focus();
    });
    document.getElementById('infoDialogClose').addEventListener('click', close);
    // A click on the backdrop (outside the box) closes too.
    dialog.addEventListener('click', e => { if (e.target === dialog) close(); });

    // `render(body)` fills the body and may return a cleanup function
    // (e.g. to release a video) that runs on close or replacement.
    function open({ heading, render, trigger, wide = false }) {
        cleanup?.(); cleanup = null;
        if (!dialog.open) returnFocus = trigger || document.activeElement;
        title.textContent = heading;
        dialog.classList.toggle('is-details', wide);
        body.replaceChildren();
        body.scrollTop = 0;
        cleanup = render(body) || null;
        if (!dialog.open) dialog.showModal();
        title.setAttribute('tabindex', '-1');
        title.focus();
    }
    return { open, close, get isOpen() { return dialog.open; } };
}

// Small DOM builders shared by help and details (textContent only, never
// innerHTML, so result strings cannot inject markup).
export function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
        if (v == null || v === false) continue;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
        else node.setAttribute(k, v === true ? '' : v);
    }
    node.append(...children.flat().filter(c => c != null && c !== false));
    return node;
}
export function factList(rows) {
    return el('dl', { class: 'facts' }, rows.filter(Boolean).flatMap(([k, v]) => [el('dt', { text: k }), el('dd', { text: v })]));
}
export function dataTable(caption, head, rows) {
    return el('div', { class: 'table-scroll', tabindex: '0', role: 'region', 'aria-label': caption },
        el('table', { class: 'data' },
            el('caption', { text: caption }),
            el('thead', {}, el('tr', {}, head.map(h => el('th', { scope: 'col', text: h })))),
            el('tbody', {}, rows.map(r => el('tr', {}, r.map((c, i) => i === 0 ? el('th', { scope: 'row', text: String(c) }) : el('td', { text: String(c) })))))));
}
