/*
 * Markdown article editor behavior (toolbar, shortcuts, live preview,
 * dirty-state tracking). Fully delegated on document so it works no matter
 * when the editor partial is swapped in by HTMX - no per-swap init needed.
 *
 * Markup contract (see partials/articles/_article_editor.html):
 *   [data-article-editor]           editor container (carries data-preview-url)
 *   [data-article-textarea]         the Markdown textarea
 *   [data-md-action="bold|..."]     toolbar buttons
 *   [data-article-preview-toggle]   preview show/hide button
 *   [data-article-preview]          preview pane (server-rendered HTML)
 *   [data-article-char-count]       live character counter
 */
(function () {
    'use strict';

    var PREVIEW_DEBOUNCE_MS = 650;
    var previewTimer = null;

    function editorRoot(el) { return el && el.closest ? el.closest('[data-article-editor]') : null; }
    function textareaOf(root) { return root ? root.querySelector('[data-article-textarea]') : null; }

    // -- Textarea editing helpers --------------------------------------------

    function replaceRange(textarea, start, end, replacement, selectStart, selectEnd) {
        textarea.focus();
        textarea.setSelectionRange(start, end);
        // execCommand keeps native undo history working where supported.
        var inserted = false;
        try { inserted = document.execCommand('insertText', false, replacement); } catch (e) { inserted = false; }
        if (!inserted) {
            textarea.value = textarea.value.slice(0, start) + replacement + textarea.value.slice(end);
        }
        textarea.setSelectionRange(selectStart, selectEnd);
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function wrapSelection(textarea, before, after, placeholder) {
        var start = textarea.selectionStart;
        var end = textarea.selectionEnd;
        var selected = textarea.value.slice(start, end) || placeholder;
        var replacement = before + selected + after;
        replaceRange(textarea, start, end, replacement, start + before.length, start + before.length + selected.length);
    }

    function prefixLines(textarea, prefix, numbered) {
        var value = textarea.value;
        var start = textarea.selectionStart;
        var end = textarea.selectionEnd;
        var lineStart = value.lastIndexOf('\n', start - 1) + 1;
        var lineEndIndex = value.indexOf('\n', end);
        var lineEnd = lineEndIndex === -1 ? value.length : lineEndIndex;
        var block = value.slice(lineStart, lineEnd);
        var lines = block.split('\n');
        var replaced = lines.map(function (line, index) {
            var p = numbered ? (index + 1) + '. ' : prefix;
            return line.length || lines.length === 1 ? p + line : line;
        }).join('\n');
        replaceRange(textarea, lineStart, lineEnd, replaced, lineStart, lineStart + replaced.length);
    }

    function insertBlock(textarea, block) {
        var start = textarea.selectionStart;
        var value = textarea.value;
        var needsLeadingBreak = start > 0 && value[start - 1] !== '\n';
        var text = (needsLeadingBreak ? '\n\n' : '') + block + '\n';
        replaceRange(textarea, start, textarea.selectionEnd, text, start + text.length, start + text.length);
    }

    function nextReferenceNumber(value) {
        var max = 0;
        var pattern = /\[\^(\d+)\]/g;
        var match;
        while ((match = pattern.exec(value)) !== null) {
            max = Math.max(max, parseInt(match[1], 10) || 0);
        }
        return max + 1;
    }

    function insertReference(textarea) {
        var n = nextReferenceNumber(textarea.value);
        var start = textarea.selectionStart;
        var marker = '[^' + n + ']';
        // Insert the marker at the cursor...
        replaceRange(textarea, start, textarea.selectionEnd, marker, start + marker.length, start + marker.length);
        // ...and append its definition at the end of the document.
        var value = textarea.value;
        var definition = (value.endsWith('\n') ? '' : '\n') + '\n[^' + n + ']: ';
        var insertAt = value.length;
        replaceRange(textarea, insertAt, insertAt, definition, insertAt + definition.length, insertAt + definition.length);
        if (window.toastr) toastr.info('Reference [' + n + '] added - fill in the source at the bottom of the article.');
    }

    var ACTIONS = {
        bold: function (t) { wrapSelection(t, '**', '**', 'bold text'); },
        italic: function (t) { wrapSelection(t, '*', '*', 'italic text'); },
        strike: function (t) { wrapSelection(t, '~~', '~~', 'struck text'); },
        h2: function (t) { prefixLines(t, '## '); },
        h3: function (t) { prefixLines(t, '### '); },
        ul: function (t) { prefixLines(t, '- '); },
        ol: function (t) { prefixLines(t, '', true); },
        quote: function (t) { prefixLines(t, '> '); },
        link: function (t) {
            var selected = t.value.slice(t.selectionStart, t.selectionEnd);
            if (/^https?:\/\//.test(selected)) {
                wrapSelection(t, '[link text](', ')', selected);
            } else {
                wrapSelection(t, '[', '](https://)', selected || 'link text');
            }
        },
        image: function (t) { wrapSelection(t, '![', '](https://image-url)', 'image description'); },
        reference: insertReference,
        code: function (t) {
            var selected = t.value.slice(t.selectionStart, t.selectionEnd);
            if (selected.indexOf('\n') !== -1) {
                wrapSelection(t, '```\n', '\n```', selected);
            } else {
                wrapSelection(t, '`', '`', selected || 'code');
            }
        },
        table: function (t) {
            insertBlock(t, '| Column | Column |\n| ------ | ------ |\n| Cell   | Cell   |');
        },
        hr: function (t) { insertBlock(t, '---'); },
    };

    // -- Live preview ---------------------------------------------------------

    function refreshPreview(root) {
        var textarea = textareaOf(root);
        var pane = root.querySelector('[data-article-preview]');
        if (!textarea || !pane || pane.hidden) return;
        var url = root.dataset.previewUrl;
        var csrf = root.querySelector('input[name="csrfmiddlewaretoken"]');
        var body = new URLSearchParams();
        body.append('content', textarea.value);
        fetch(url, {
            method: 'POST',
            headers: { 'X-CSRFToken': csrf ? csrf.value : '' },
            body: body,
            credentials: 'same-origin',
        }).then(function (resp) {
            if (!resp.ok) throw new Error('HTTP ' + resp.status);
            return resp.text();
        }).then(function (html) {
            var inner = pane.querySelector('.article-editor-preview-inner');
            if (inner) inner.innerHTML = html;
        }).catch(function () {
            var inner = pane.querySelector('.article-editor-preview-inner');
            if (inner) inner.innerHTML = '<p class="article-preview-empty">Preview unavailable right now.</p>';
        });
    }

    function schedulePreview(root) {
        if (previewTimer) window.clearTimeout(previewTimer);
        previewTimer = window.setTimeout(function () { refreshPreview(root); }, PREVIEW_DEBOUNCE_MS);
    }

    function updateCharCount(root) {
        var textarea = textareaOf(root);
        var counter = root.querySelector('[data-article-char-count]');
        if (!textarea || !counter) return;
        var max = parseInt(textarea.getAttribute('maxlength') || '0', 10);
        var length = textarea.value.length;
        counter.textContent = max ? length.toLocaleString() + ' / ' + max.toLocaleString() : length.toLocaleString();
        counter.classList.toggle('article-char-count--warn', max > 0 && length > max * 0.95);
    }

    // -- Delegated wiring -----------------------------------------------------

    document.addEventListener('click', function (event) {
        var toolButton = event.target.closest ? event.target.closest('[data-md-action]') : null;
        if (toolButton) {
            var root = editorRoot(toolButton);
            var textarea = textareaOf(root);
            var action = ACTIONS[toolButton.dataset.mdAction];
            if (root && textarea && action) {
                event.preventDefault();
                action(textarea);
            }
            return;
        }
        var previewToggle = event.target.closest ? event.target.closest('[data-article-preview-toggle]') : null;
        if (previewToggle) {
            var toggleRoot = editorRoot(previewToggle);
            var pane = toggleRoot ? toggleRoot.querySelector('[data-article-preview]') : null;
            if (!pane) return;
            pane.hidden = !pane.hidden;
            previewToggle.classList.toggle('is-active', !pane.hidden);
            toggleRoot.classList.toggle('article-panel--preview-open', !pane.hidden);
            if (!pane.hidden) refreshPreview(toggleRoot);
        }
    });

    document.addEventListener('input', function (event) {
        var root = editorRoot(event.target);
        if (!root || !event.target.matches('[data-article-textarea]')) return;
        root.dataset.dirty = '1';
        updateCharCount(root);
        schedulePreview(root);
    });

    document.addEventListener('keydown', function (event) {
        if (!event.target.matches || !event.target.matches('[data-article-textarea]')) return;
        if (!(event.ctrlKey || event.metaKey)) return;
        var key = event.key.toLowerCase();
        var map = { b: 'bold', i: 'italic', k: 'link' };
        if (!map[key]) return;
        event.preventDefault();
        ACTIONS[map[key]](event.target);
    });

    // Skip the Cancel button's "discard changes?" prompt when nothing changed.
    document.addEventListener('htmx:confirm', function (event) {
        var el = event.detail ? event.detail.elt : null;
        var root = editorRoot(el);
        if (!root || !el.matches('[hx-get]')) return;
        if (root.dataset.dirty !== '1') {
            event.preventDefault();
            event.detail.issueRequest(true);
        }
    });

    // Initialize the counter whenever an editor is swapped in.
    document.body.addEventListener('htmx:afterSwap', function () {
        document.querySelectorAll('[data-article-editor]').forEach(updateCharCount);
    });

    // Warn before navigating away from an editor with unsaved changes.
    window.addEventListener('beforeunload', function (event) {
        var dirty = document.querySelector('[data-article-editor][data-dirty="1"]');
        if (dirty) {
            event.preventDefault();
            event.returnValue = '';
        }
    });
}());
