// Scripts run in the EduRec frame. Playwright evaluates this file as one function:
// `edurec.run_script` passes {command, ...arguments} and gets back the command's result.
({command, ...args}) => {
    // Fill a PeopleSoft field and fire the events its delegated handlers listen for.
    const setBox = (box, value) => {
        box.value = value;
        for (const type of ['input', 'change']) box.dispatchEvent(new Event(type, {bubbles: true}));
    };
    const review = () => window.__edurecReview;
    const stateNumber = () => document.getElementById('ICStateNum');

    const commands = {
        // A postback finished: the state number moved, the loader is idle and `target` exists.
        settled: ({old, target}) => {
            const state = stateNumber();
            return !!state && state.value !== old &&
                !(typeof isLoaderInProcess === 'function' && isLoaderInProcess()) &&
                (!target || !!document.getElementById(target));
        },
        // The panel's Skip was pressed, or the state changed: an EduRec button was
        // pressed, or PeopleSoft re-rendered the page on an innocuous interaction.
        signalled: ({old}) => {
            const state = stateNumber();
            return !!review()?.skipped || !!(state && state.value !== old);
        },
        skipped: () => (review()?.skipped ? review().state.skipReason : null),
        clicked: () => review()?.clicked ?? null,
        comment: ({id, value}) => {
            const box = document.getElementById(id);
            if (!box) throw new Error('Comment box not found');
            setBox(box, value);
        },
        install,
    };
    return commands[command](args);

    function install({
        panel, buttons, cancel, comment_box: commentBox, verdicts, labels, prefills, colours,
        dry_run: dryRun, fresh,
    }) {
        const prior = review();
        prior?.unhook();
        const hooks = [];
        const listen = (target, type, handler) => {
            target.addEventListener(type, handler, true);
            hooks.push([target, type, handler]);
        };
        // The panel's state outlives a PeopleSoft re-render; only a fresh prepare resets it.
        const initial = {selected: 'recommended', viewed: 'recommended', scrollTop: 0, skipReason: ''};
        const state = !fresh && prior ? prior.state : initial;
        const current = window.__edurecReview = {
            skipped: false, clicked: null, state,
            unhook: () => hooks.forEach(([t, type, h]) => t.removeEventListener(type, h, true)),
        };
        const box = document.getElementById(commentBox);
        document.getElementById('edurec-review-panel')?.remove();
        // A shadow root keeps the page's CSS off the panel.
        const host = document.createElement('div');
        host.id = 'edurec-review-panel';
        const root = host.attachShadow({mode: 'open'});
        root.innerHTML = panel;
        document.body.appendChild(host);
        const $ = selector => root.querySelector(selector);
        const $$ = selector => [...root.querySelectorAll(selector)];
        const block = event => { event.preventDefault(); event.stopImmediatePropagation(); };

        const names = {recommended: 'Recommended', fallback: 'Fallback'};
        const modified = () => !!box && box.value.trim() !== (prefills[state.selected] || '').trim();
        const mark = () => { for (const el of $$('.modified')) el.hidden = !modified(); };
        const fill = value => {
            if (!box) return;
            setBox(box, value);
            mark();
        };
        // Viewing a tab shows its pane; selecting one decides the comment, outline and pill.
        const view = tab => {
            state.viewed = tab;
            for (const el of $$('.tab, .pane')) el.classList.toggle('active', el.dataset.tab === tab);
        };
        const choose = tab => {
            state.selected = tab;
            for (const el of $$('.pill[data-tab]')) {
                el.classList.toggle('active', el.dataset.tab === tab);
            }
            for (const el of $$('.pane')) el.classList.toggle('selected', el.dataset.tab === tab);
            for (const el of $$('.select')) {
                el.disabled = el.closest('.pane').dataset.tab === tab;
                el.textContent = el.disabled ? 'Selected' : 'Select';
            }
            for (const [id, verdict] of Object.entries(buttons)) {
                const button = document.getElementById(id);
                const active = verdict === verdicts[tab];
                if (button) button.style.outline = active ? `3px solid ${colours[verdict]}` : '';
            }
            mark();
        };
        const select = tab => {
            if (tab === state.selected) return;
            const question = `The comment box differs from the ${names[state.selected]} comment. ` +
                `Replace it with the ${names[tab]} comment?`;
            if (modified() && !window.confirm(question)) return;
            choose(tab);
            fill(prefills[tab]);
        };

        for (const tab of $$('.tab')) tab.onclick = () => view(tab.dataset.tab);
        for (const el of $$('.select')) el.onclick = () => select(el.closest('.pane').dataset.tab);
        for (const reset of $$('.reset')) reset.onclick = () => fill(prefills[state.selected]);
        if (box) listen(box, 'input', mark);
        const reason = $('#reason');
        reason.value = state.skipReason;
        reason.oninput = () => { state.skipReason = reason.value; };
        $('#skip').onclick = () => { current.skipped = true; };
        choose(state.selected);
        view(state.viewed);
        const body = $('#body');  // Scroll after the tab is shown, or anchoring shifts the offset.
        body.scrollTop = state.scrollTop;
        body.onscroll = () => { state.scrollTop = body.scrollTop; };

        for (const id of [...Object.keys(buttons), cancel]) {
            const button = document.getElementById(id);
            if (!button) continue;
            if (id !== cancel) button.disabled = dryRun;
            listen(button, 'click', event => {
                const verdict = buttons[id];
                if (dryRun && verdict) {
                    block(event);
                    window.alert('Dry run: nothing is submitted');
                    return;
                }
                if (verdict && verdict !== verdicts[state.selected]) {
                    if (verdict === verdicts.fallback) {
                        const question =
                            'This matches the fallback. Switch to the fallback comment and submit?';
                        if (!window.confirm(question)) return block(event);
                        choose('fallback');
                        fill(prefills.fallback);
                    } else {
                        const label =
                            state.selected === 'fallback' ? 'Fallback selected' : 'Recommended';
                        const chosen = labels[verdicts[state.selected]];
                        const question = `${label}: ${chosen}. Submit ${button.value} anyway?`;
                        if (!window.confirm(question)) return block(event);
                    }
                }
                current.clicked = {id, comment: box ? box.value : null};
            });
        }
    }
}
