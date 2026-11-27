/* Azom Ops Dashboard — strict CSP-safe client runtime (no eval, no inline handlers) */
(function () {
  'use strict';

  var lgMq = window.matchMedia('(min-width: 1024px)');

  /* ------------------------------------------------------------------ */
  /* 1. Mobile Navigation Drawer                                        */
  /* ------------------------------------------------------------------ */
  function initDrawer() {
    var drawer = document.getElementById('navDrawer');
    var overlay = document.getElementById('navOverlay');
    var openBtn = document.getElementById('navToggle');
    var closeBtn = document.getElementById('navClose');
    var mainCol = document.getElementById('appMain');
    if (!drawer || !openBtn) return;

    var isOpen = false;

    function syncInert(open) {
      if (!mainCol) return;
      if (open && !lgMq.matches) {
        mainCol.setAttribute('inert', '');
      } else {
        mainCol.removeAttribute('inert');
      }
    }

    function openDrawer() {
      if (lgMq.matches) return;
      isOpen = true;
      drawer.classList.remove('-translate-x-full');
      drawer.classList.add('translate-x-0');
      drawer.setAttribute('aria-modal', 'true');
      drawer.setAttribute('role', 'dialog');
      if (overlay) {
        overlay.classList.remove('hidden');
      }
      openBtn.setAttribute('aria-expanded', 'true');
      syncInert(true);
      if (closeBtn) closeBtn.focus();
    }

    function closeDrawer(restoreFocus) {
      if (!isOpen && !lgMq.matches) {
        syncInert(false);
        return;
      }
      isOpen = false;
      drawer.classList.add('-translate-x-full');
      drawer.classList.remove('translate-x-0');
      drawer.removeAttribute('aria-modal');
      drawer.removeAttribute('role');
      if (overlay) {
        overlay.classList.add('hidden');
      }
      openBtn.setAttribute('aria-expanded', 'false');
      syncInert(false);
      if (restoreFocus && !lgMq.matches) openBtn.focus();
    }

    openBtn.addEventListener('click', function () {
      if (isOpen) closeDrawer(true);
      else openDrawer();
    });
    if (closeBtn) {
      closeBtn.addEventListener('click', function () {
        closeDrawer(true);
      });
    }
    if (overlay) {
      overlay.addEventListener('click', function () {
        closeDrawer(true);
      });
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isOpen) {
        e.preventDefault();
        closeDrawer(true);
      }
    });
    if (lgMq.addEventListener) {
      lgMq.addEventListener('change', function (e) {
        if (e.matches) closeDrawer(false);
      });
    }
  }

  /* ------------------------------------------------------------------ */
  /* 2. Native <dialog> light-dismiss fallback (Safari)                 */
  /* ------------------------------------------------------------------ */
  function initDialogLightDismiss() {
    if ('closedBy' in HTMLDialogElement.prototype) return;
    function outside(e, dialog) {
      var r = dialog.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    }
    document.querySelectorAll('dialog[closedby="any"]').forEach(function (dialog) {
      var pressedOutside = false;
      dialog.addEventListener('pointerdown', function (e) {
        pressedOutside = outside(e, dialog);
      });
      dialog.addEventListener('pointerup', function (e) {
        if (pressedOutside && outside(e, dialog)) dialog.close();
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* 3. Command Palette (<dialog id="az-cmd-dialog">)                   */
  /* ------------------------------------------------------------------ */
  function initCommandPalette() {
    var dialog = document.getElementById('az-cmd-dialog');
    var input = document.getElementById('az-cmd-input');
    var list = document.getElementById('az-cmd-list');
    var empty = document.getElementById('az-cmd-empty');
    if (!dialog || !input || !list) return;

    function collectRoutes() {
      var seen = new Set();
      var items = [];
      document.querySelectorAll('#navDrawer nav a[href]').forEach(function (a) {
        var href = a.getAttribute('href') || '';
        if (!href || seen.has(href)) return;
        seen.add(href);
        var groupEl = a.closest('div[data-nav-group]');
        var group = groupEl ? groupEl.getAttribute('data-nav-group') : '';
        var labelEl = a.querySelector('[data-nav-label]');
        var label = (labelEl ? labelEl.textContent : a.textContent || '').replace(/\s+/g, ' ').trim();
        items.push({
          href: href,
          label: label,
          group: group,
          search: (group + ' ' + label + ' ' + href).toLowerCase(),
        });
      });
      return items;
    }

    var allRoutes = collectRoutes();
    var activeIdx = 0;
    var visibleItems = [];

    function render(query) {
      var q = (query || '').trim().toLowerCase();
      visibleItems = q
        ? allRoutes.filter(function (item) {
            return item.search.indexOf(q) !== -1;
          })
        : allRoutes.slice();
      activeIdx = 0;
      list.textContent = '';

      if (visibleItems.length === 0) {
        if (empty) empty.classList.remove('hidden');
        return;
      }
      if (empty) empty.classList.add('hidden');

      visibleItems.forEach(function (item, idx) {
        var li = document.createElement('li');
        var a = document.createElement('a');
        a.href = item.href;
        a.className =
          'flex items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm transition ' +
          (idx === activeIdx
            ? 'bg-ink-800 text-steel-200 ring-1 ring-steel-500/30'
            : 'text-mist-200 hover:bg-ink-800/80 hover:text-mist-100');
        a.setAttribute('data-cmd-idx', String(idx));

        var left = document.createElement('span');
        left.className = 'flex items-center gap-2 min-w-0';
        if (item.group) {
          var grp = document.createElement('span');
          grp.className = 'text-xs text-mist-500 shrink-0';
          grp.textContent = item.group;
          left.appendChild(grp);
        }
        var lbl = document.createElement('span');
        lbl.className = 'truncate font-medium';
        lbl.textContent = item.label;
        left.appendChild(lbl);

        var path = document.createElement('code');
        path.className = 'font-mono text-xs text-mist-400 shrink-0';
        path.textContent = item.href;

        a.appendChild(left);
        a.appendChild(path);
        li.appendChild(a);
        list.appendChild(li);
      });
    }

    function highlight(idx) {
      var links = list.querySelectorAll('a[data-cmd-idx]');
      links.forEach(function (a, i) {
        if (i === idx) {
          a.className =
            'flex items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm transition bg-ink-800 text-steel-200 ring-1 ring-steel-500/30';
          a.scrollIntoView({ block: 'nearest' });
        } else {
          a.className =
            'flex items-center justify-between gap-3 rounded-lg px-3 py-2 text-sm transition text-mist-200 hover:bg-ink-800/80 hover:text-mist-100';
        }
      });
    }

    function openPalette() {
      allRoutes = collectRoutes();
      input.value = '';
      render('');
      if (!dialog.open) dialog.showModal();
      input.focus();
    }

    document.querySelectorAll('[data-cmd-open]').forEach(function (btn) {
      btn.addEventListener('click', openPalette);
    });

    document.addEventListener('keydown', function (e) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        if (dialog.open) dialog.close();
        else openPalette();
      }
    });

    input.addEventListener('input', function () {
      render(input.value);
    });

    input.addEventListener('keydown', function (e) {
      if (!visibleItems.length) return;
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        activeIdx = (activeIdx + 1) % visibleItems.length;
        highlight(activeIdx);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        activeIdx = (activeIdx - 1 + visibleItems.length) % visibleItems.length;
        highlight(activeIdx);
      } else if (e.key === 'Enter') {
        e.preventDefault();
        var target = visibleItems[activeIdx];
        if (target && target.href) {
          dialog.close();
          window.location.assign(target.href);
        }
      }
    });

    dialog.addEventListener('close', function () {
      dialog.returnValue = '';
    });
  }

  /* ------------------------------------------------------------------ */
  /* 4. HITL Confirmation Dialog + Busy-Label Submit Guard              */
  /* ------------------------------------------------------------------ */
  function applyBusyState(form, submitter) {
    var busyLabel =
      (submitter && submitter.getAttribute('data-busy-label')) ||
      form.getAttribute('data-busy-label');
    if (!busyLabel) return;

    if (submitter && submitter.name && !form.querySelector('input[data-busy-injected="1"]')) {
      var hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = submitter.name;
      hidden.value = submitter.value || '';
      hidden.setAttribute('data-busy-injected', '1');
      form.appendChild(hidden);
    }

    form.querySelectorAll('button[type="submit"], input[type="submit"]').forEach(function (btn) {
      if (btn === submitter) {
        if (!btn.hasAttribute('data-orig-label')) {
          btn.setAttribute('data-orig-label', btn.textContent || '');
        }
        btn.textContent = busyLabel;
      }
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
    });
  }

  function initConfirmAndBusy() {
    var dialog = document.getElementById('az-confirm-dialog');
    var titleEl = document.getElementById('az-confirm-title');
    var msgEl = document.getElementById('az-confirm-msg');
    var okBtn = document.getElementById('az-confirm-ok');
    var pendingSubmit = null;

    document.addEventListener('click', function (e) {
      var closeTrigger = e.target.closest('[data-dialog-close]');
      if (closeTrigger) {
        var dlg = closeTrigger.closest('dialog');
        if (dlg && dlg.open) dlg.close('cancel');
        return;
      }
      var acceptTrigger = e.target.closest('[data-confirm-accept]');
      if (acceptTrigger && dialog && dialog.open) {
        dialog.close('confirm');
      }
    });

    if (dialog) {
      dialog.addEventListener('close', function () {
        var val = dialog.returnValue;
        dialog.returnValue = '';
        if (val === 'confirm' && pendingSubmit) {
          var form = pendingSubmit.form;
          var submitter = pendingSubmit.submitter;
          pendingSubmit = null;
          form.setAttribute('data-az-confirmed', '1');
          applyBusyState(form, submitter);
          if (
            submitter &&
            submitter.name &&
            !form.querySelector('input[data-busy-injected="1"]') &&
            !form.querySelector('input[data-confirm-injected="1"]')
          ) {
            var hidden = document.createElement('input');
            hidden.type = 'hidden';
            hidden.name = submitter.name;
            hidden.value = submitter.value || '';
            hidden.setAttribute('data-confirm-injected', '1');
            form.appendChild(hidden);
          }
          if (typeof form.requestSubmit === 'function' && (!submitter || !submitter.disabled)) {
            form.requestSubmit(submitter || undefined);
          } else {
            form.removeAttribute('data-az-confirmed');
            HTMLFormElement.prototype.submit.call(form);
          }
        } else {
          pendingSubmit = null;
        }
      });
    }

    document.addEventListener('submit', function (e) {
      var form = e.target;
      if (!(form instanceof HTMLFormElement)) return;
      var submitter = e.submitter || null;

      // Check confirmation unless already confirmed in this submission cycle
      if (form.getAttribute('data-az-confirmed') === '1') {
        form.removeAttribute('data-az-confirmed');
        applyBusyState(form, submitter);
        return;
      }

      var confirmMsg =
        (submitter && submitter.getAttribute('data-confirm')) ||
        form.getAttribute('data-confirm') ||
        '';

      // Support legacy data-approve-guard on case_detail textarea without inline JS
      if (!confirmMsg && submitter && (submitter.value === 'reply' || submitter.value === 'reply_next')) {
        var guard = form.querySelector('[data-approve-guard]');
        if (guard) {
          confirmMsg =
            guard.getAttribute('data-approve-confirm') ||
            submitter.getAttribute('data-approve-confirm') ||
            'Skicka svar till kunden nu?';
        }
      }

      if (confirmMsg) {
        e.preventDefault();
        if (dialog && msgEl) {
          var customTitle =
            (submitter && submitter.getAttribute('data-confirm-title')) ||
            form.getAttribute('data-confirm-title') ||
            'Bekräfta åtgärd';
          var customOk =
            (submitter && submitter.getAttribute('data-confirm-ok')) ||
            form.getAttribute('data-confirm-ok') ||
            'Bekräfta';
          var isDanger =
            (submitter && submitter.getAttribute('data-confirm-danger') === '1') ||
            form.getAttribute('data-confirm-danger') === '1';

          if (titleEl) titleEl.textContent = customTitle;
          msgEl.textContent = confirmMsg;
          if (okBtn) {
            okBtn.textContent = customOk;
            okBtn.className = isDanger ? 'az-btn-danger' : 'az-btn-primary';
          }
          pendingSubmit = { form: form, submitter: submitter };
          dialog.showModal();
        } else if (window.confirm(confirmMsg)) {
          form.setAttribute('data-az-confirmed', '1');
          applyBusyState(form, submitter);
          if (typeof form.requestSubmit === 'function' && (!submitter || !submitter.disabled)) {
            form.requestSubmit(submitter || undefined);
          } else {
            form.removeAttribute('data-az-confirmed');
            HTMLFormElement.prototype.submit.call(form);
          }
        }
        return;
      }

      applyBusyState(form, submitter);
    });

    // Reset disabled buttons and HITL confirmation state if browser restores page from bfcache
    window.addEventListener('pageshow', function (e) {
      document.querySelectorAll('form[data-az-confirmed]').forEach(function (f) {
        f.removeAttribute('data-az-confirmed');
      });
      if (!e.persisted) return;
      document.querySelectorAll('input[data-busy-injected="1"], input[data-confirm-injected="1"]').forEach(function (el) {
        el.remove();
      });
      document.querySelectorAll('button[data-orig-label]').forEach(function (btn) {
        btn.textContent = btn.getAttribute('data-orig-label');
        btn.disabled = false;
        btn.removeAttribute('aria-busy');
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* 5. Utility Data-Hooks                                              */
  /* ------------------------------------------------------------------ */
  function initUtilityHooks() {
    // Dismiss flash banner
    document.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-dismiss-flash]');
      if (!btn) return;
      var banner = btn.closest('[data-flash-banner], .az-flash');
      if (banner) banner.remove();
    });

    // Password reveal toggle
    document.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-toggle-secret]');
      if (!btn) return;
      var targetId = btn.getAttribute('data-toggle-secret');
      var input = targetId ? document.getElementById(targetId) : null;
      if (!input) {
        var wrap = btn.closest('[data-secret-field]') || btn.parentElement;
        input = wrap ? wrap.querySelector('input') : null;
      }
      if (!input) return;
      var reveal = input.type === 'password';
      input.type = reveal ? 'text' : 'password';
      btn.setAttribute('aria-pressed', reveal ? 'true' : 'false');
      var label = btn.querySelector('[data-toggle-label]');
      if (label) {
        label.textContent = reveal ? 'Dölj' : 'Visa';
      } else {
        btn.setAttribute('aria-label', reveal ? 'Dölj värde' : 'Visa värde');
      }
      var eyeOpen = btn.querySelector('[data-eye-open]');
      var eyeOff = btn.querySelector('[data-eye-off]');
      if (eyeOpen && eyeOff) {
        eyeOpen.classList.toggle('hidden', reveal);
        eyeOff.classList.toggle('hidden', !reveal);
      }
    });

    // Copy to clipboard
    document.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-copy], [data-copy-target]');
      if (!btn) return;
      var text = btn.getAttribute('data-copy');
      if (text === null) {
        var sel = btn.getAttribute('data-copy-target');
        var el = sel ? document.querySelector(sel) : null;
        if (!el) return;
        text = 'value' in el && el.value !== undefined ? String(el.value) : (el.textContent || '');
      }
      function markCopied() {
        var labelEl = btn.querySelector('[data-copy-label]');
        var textTarget = labelEl;
        if (!textTarget) {
          if (btn.querySelector('svg')) {
            var span = document.createElement('span');
            span.setAttribute('data-copy-label', '1');
            Array.prototype.slice.call(btn.childNodes).forEach(function (node) {
              if (node.nodeType === 3) span.appendChild(node);
            });
            btn.appendChild(span);
            textTarget = span;
          } else {
            textTarget = btn;
          }
        }
        var orig = btn.getAttribute('data-orig-copy');
        if (orig === null) {
          orig = (textTarget.textContent || '').trim();
          btn.setAttribute('data-orig-copy', orig);
        }
        textTarget.textContent = 'Kopierad';
        window.setTimeout(function () {
          textTarget.textContent = btn.getAttribute('data-orig-copy') || '';
        }, 1800);
      }
      function fallbackCopy() {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try {
          if (document.execCommand('copy')) {
            markCopied();
          }
        } catch (_err) {}
        ta.remove();
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(markCopied).catch(fallbackCopy);
      } else {
        fallbackCopy();
      }
    });

    // Live character counter
    function updateCharCounter(el) {
      var sel = el.getAttribute('data-char-count');
      var out = sel ? document.querySelector(sel) : null;
      if (!out && el.parentElement) {
        out = el.parentElement.querySelector('[data-char-count-out]');
      }
      if (!out) return;
      var len = (el.value || '').length;
      var max = el.getAttribute('maxlength');
      out.textContent = max ? len + ' / ' + max : len + ' tecken';
    }
    document.querySelectorAll('[data-char-count]').forEach(updateCharCounter);
    document.addEventListener('input', function (e) {
      if (e.target && e.target.hasAttribute && e.target.hasAttribute('data-char-count')) {
        updateCharCounter(e.target);
      }
    });

    // Auto-submit select
    document.addEventListener('change', function (e) {
      var sel = e.target.closest('select[data-auto-submit]');
      if (!sel || !sel.form) return;
      if (typeof sel.form.requestSubmit === 'function') sel.form.requestSubmit();
      else sel.form.submit();
    });

    // Bulk select-all checkbox with indeterminate state
    function syncBulkGroup(group) {
      var selectorAll = group ? '[data-bulk-all="' + group + '"]' : '[data-bulk-all]';
      var selectorItem = group ? '[data-bulk-item="' + group + '"]' : '[data-bulk-item]';
      var allBox = document.querySelector(selectorAll);
      var items = Array.prototype.slice.call(document.querySelectorAll(selectorItem));
      if (!items.length) return;
      var checkedCount = items.filter(function (c) {
        return c.checked;
      }).length;
      if (allBox) {
        allBox.checked = checkedCount === items.length;
        allBox.indeterminate = checkedCount > 0 && checkedCount < items.length;
      }
      document.querySelectorAll('[data-bulk-count]').forEach(function (badge) {
        var bGroup = badge.getAttribute('data-bulk-count') || '';
        if (bGroup === (group || '')) badge.textContent = String(checkedCount);
      });
      document.querySelectorAll('[data-bulk-submit]').forEach(function (btn) {
        var bGroup = btn.getAttribute('data-bulk-submit') || '';
        if (bGroup === (group || '')) btn.disabled = checkedCount === 0;
      });
    }
    document.querySelectorAll('[data-bulk-all]').forEach(function (box) {
      syncBulkGroup(box.getAttribute('data-bulk-all') || '');
    });
    document.addEventListener('change', function (e) {
      var allBox = e.target.closest('[data-bulk-all]');
      if (allBox) {
        var grp = allBox.getAttribute('data-bulk-all') || '';
        var sel = grp ? '[data-bulk-item="' + grp + '"]' : '[data-bulk-item]';
        document.querySelectorAll(sel).forEach(function (item) {
          item.checked = allBox.checked;
        });
        syncBulkGroup(grp);
        return;
      }
      var itemBox = e.target.closest('[data-bulk-item]');
      if (itemBox) {
        syncBulkGroup(itemBox.getAttribute('data-bulk-item') || '');
      }
    });

    // Dynamic model-profile datalist updater
    document.addEventListener('change', function (e) {
      var sel = e.target.closest('select[data-route-select]');
      if (!sel) return;
      var targetSel = sel.getAttribute('data-model-target');
      var input = targetSel ? document.querySelector(targetSel) : null;
      if (!input) {
        var row = sel.closest('tr');
        input = row ? row.querySelector('input[name="model_id"]') : null;
      }
      if (input && sel.value) {
        input.setAttribute('list', 'rec-' + sel.value);
      }
    });

    // Catalog model quick-fill (replaces inline script in oscar.html)
    document.addEventListener('click', function (e) {
      var btn = e.target.closest('[data-model-id][data-model-target]');
      if (!btn) return;
      var field = document.getElementById(btn.getAttribute('data-model-target'));
      if (field) {
        field.value = btn.getAttribute('data-model-id') || '';
        field.focus();
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* 6. xAI Device Login Polling (vanilla fallback + Alpine.data)       */
  /* ------------------------------------------------------------------ */
  function allowXaiUri(uri) {
    return typeof uri === 'string' &&
      (uri.indexOf('https://accounts.x.ai/') === 0 || uri.indexOf('https://auth.x.ai/') === 0);
  }

  function initVanillaXaiPolling() {
    var box = document.getElementById('xai-login-status');
    if (!box || box.hasAttribute('x-data') || box.getAttribute('data-status') !== 'pending') return;
    var pollUrl = box.getAttribute('data-xai-poll-url') || '/oscar/models/xai/login/status';
    var tries = 0;

    function setText(id, val) {
      var node = document.getElementById(id);
      if (node) node.textContent = val || '';
    }

    function tick() {
      tries += 1;
      fetch(pollUrl, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      })
        .then(function (res) {
          if (!res.ok) throw new Error('status');
          return res.json();
        })
        .then(function (body) {
          var st = String(body.status || '');
          box.setAttribute('data-status', st);
          setText('xai-login-state', st);
          setText('xai-login-code', body.user_code || '');
          var link = document.getElementById('xai-login-link');
          var uri = String(body.verification_uri || '');
          if (link) {
            if (allowXaiUri(uri)) {
              link.href = uri;
              link.target = '_blank';
              link.rel = 'noopener noreferrer';
              link.hidden = false;
            } else {
              link.hidden = true;
              link.removeAttribute('href');
            }
          }
          if (st === 'finished') {
            setText('xai-login-detail', 'Inloggningen är klar. Grok Build är aktiverat.');
            return;
          }
          if (st === 'failed') {
            setText('xai-login-detail', body.detail || 'Inloggningen misslyckades.');
            return;
          }
          setText('xai-login-detail', 'Väntar på godkännande i webbläsaren. ' + (body.detail || ''));
          if (tries < 150) window.setTimeout(tick, 2000);
          else setText('xai-login-detail', 'Sidan slutade vänta. Ladda om för att se läget.');
        })
        .catch(function () {
          if (tries < 150) window.setTimeout(tick, 2000);
        });
    }

    window.setTimeout(tick, 2000);
  }

  /* ------------------------------------------------------------------ */
  /* 7. @alpinejs/csp Data Components                                   */
  /* ------------------------------------------------------------------ */
  function registerAlpineComponents() {
    if (!window.Alpine || typeof window.Alpine.data !== 'function') return;

    function createOnboardingState() {
      return {
        loading: false,
        mounted: false,
        notMounted: true,
        errorMsg: '',
        hasError: false,
        healthOk: true,
        notHealthOk: false,
        gmailConnected: false,
        notGmailConnected: true,
        gmailEmail: '',
        secrets: [],
        checks: [],
        secretsHint: 'checklista',
        refreshLabel: 'Uppdatera status',
        gmailStatusText: 'Inte ansluten',
        healthStatusText: 'OK',
        healthStatusClass: 'az-present',
        gmailStatusClass: 'az-missing',
        statusUrl: '/onboarding/status',
        gmailUrl: '/oauth/gmail/status',

        _normalizeSecrets: function (list) {
          return (list || []).map(function (s) {
            var present = Boolean(s && s.present);
            return {
              group: (s && s.group) || '',
              name: (s && s.name) || '',
              present: present,
              notPresent: !present,
              statusLabel: present ? 'present' : 'missing',
              statusClass: present ? 'az-present' : 'az-missing',
            };
          });
        },

        _normalizeChecks: function (list) {
          return (list || []).map(function (c) {
            var ok = Boolean(c && c.ok);
            return {
              name: (c && c.name) || '',
              ok: ok,
              notOk: !ok,
              detail: (c && c.detail) || '',
              statusLabel: ok ? 'ok' : 'fail',
              statusClass: ok ? 'az-present' : 'az-missing',
            };
          });
        },

        _syncComputed: function () {
          this.notMounted = !this.mounted;
          this.hasError = Boolean(this.errorMsg);
          this.notHealthOk = !this.healthOk;
          this.notGmailConnected = !this.gmailConnected;
          this.refreshLabel = this.loading ? 'Uppdaterar…' : 'Uppdatera status';
          this.gmailStatusText = this.gmailConnected
            ? (this.gmailEmail ? 'Ansluten · ' + this.gmailEmail : 'Ansluten')
            : 'Inte ansluten';
          this.gmailStatusClass = this.gmailConnected ? 'az-present' : 'az-missing';
          this.healthStatusText = this.healthOk ? 'OK' : 'Problem';
          this.healthStatusClass = this.healthOk ? 'az-present' : 'az-missing';
          var missing = this.secrets.filter(function (s) {
            return !s.present;
          }).length;
          this.secretsHint = missing ? missing + ' saknas' : 'alla konfigurerade';
        },

        init: function () {
          var initScript = document.getElementById('onboarding-init');
          var raw =
            (initScript && initScript.textContent) ||
            (this.$el && this.$el.getAttribute('data-onboarding')) ||
            '';
          if (raw) {
            try {
              var parsed = JSON.parse(raw);
              if (typeof parsed.healthOk === 'boolean') this.healthOk = parsed.healthOk;
              if (typeof parsed.gmailConnected === 'boolean') this.gmailConnected = parsed.gmailConnected;
              if (parsed.gmailEmail) this.gmailEmail = String(parsed.gmailEmail);
              if (Array.isArray(parsed.secrets)) this.secrets = this._normalizeSecrets(parsed.secrets);
              if (Array.isArray(parsed.checks)) this.checks = this._normalizeChecks(parsed.checks);
            } catch (_err) {}
          }
          if (this.$el) {
            this.statusUrl = this.$el.getAttribute('data-status-url') || this.statusUrl;
            this.gmailUrl = this.$el.getAttribute('data-gmail-url') || this.gmailUrl;
          }
          this.mounted = true;
          this._syncComputed();
          this.refresh();
        },

        refresh: async function () {
          this.loading = true;
          this.errorMsg = '';
          this._syncComputed();
          try {
            var responses = await Promise.all([
              fetch(this.statusUrl, { credentials: 'same-origin' }),
              fetch(this.gmailUrl, { credentials: 'same-origin' }),
            ]);
            if (!responses[0].ok || !responses[1].ok) {
              throw new Error('Statushämtning misslyckades (HTTP ' + responses[0].status + ')');
            }
            var data = await Promise.all([responses[0].json(), responses[1].json()]);
            var st = data[0] || {};
            var gm = data[1] || {};
            this.healthOk = Boolean(st.health && st.health.ok);
            if (st.health && Array.isArray(st.health.checks)) {
              this.checks = this._normalizeChecks(st.health.checks);
            }
            if (Array.isArray(st.secrets)) {
              this.secrets = this._normalizeSecrets(st.secrets);
            }
            this.gmailConnected = Boolean(gm.connected);
            this.gmailEmail = gm.email ? String(gm.email) : '';
          } catch (err) {
            this.errorMsg = 'Kunde inte uppdatera status just nu. Försök igen om en stund.';
          }
          this.loading = false;
          this._syncComputed();
        },
      };
    }

    window.Alpine.data('onboardingView', createOnboardingState);
    window.Alpine.data('onboardingPanel', createOnboardingState);

    window.Alpine.data('xaiPolling', function () {
      return {
        status: '',
        userCode: '',
        verificationUri: '',
        detail: '',
        pollUrl: '/oscar/models/xai/login/status',
        isPending: false,
        isFinished: false,
        isFailed: false,
        hasUri: false,
        _tries: 0,

        _sync: function () {
          this.isPending = this.status === 'pending';
          this.isFinished = this.status === 'finished';
          this.isFailed = this.status === 'failed';
          this.hasUri = allowXaiUri(this.verificationUri);
          if (this.isFinished) {
            this.detail = 'Inloggningen är klar. Grok Build är aktiverat.';
          }
        },

        init: function () {
          if (this.$el) {
            this.status = this.$el.getAttribute('data-status') || '';
            this.userCode = this.$el.getAttribute('data-user-code') || '';
            this.verificationUri = this.$el.getAttribute('data-verification-uri') || '';
            this.detail = this.$el.getAttribute('data-detail') || '';
            this.pollUrl = this.$el.getAttribute('data-xai-poll-url') || this.pollUrl;
          }
          this._sync();
          if (this.isPending) {
            var self = this;
            window.setTimeout(function () {
              self.tick();
            }, 2000);
          }
        },

        tick: async function () {
          this._tries += 1;
          try {
            var res = await fetch(this.pollUrl, {
              credentials: 'same-origin',
              headers: { Accept: 'application/json' },
            });
            if (!res.ok) throw new Error('status');
            var body = await res.json();
            this.status = String(body.status || '');
            this.userCode = body.user_code ? String(body.user_code) : '';
            this.verificationUri = allowXaiUri(body.verification_uri) ? String(body.verification_uri) : '';
            this.detail = body.detail ? String(body.detail) : '';
            if (this.$el) this.$el.setAttribute('data-status', this.status);
            this._sync();
            if (this.isPending) {
              this.detail = 'Väntar på godkännande i webbläsaren. ' + this.detail;
              if (this._tries < 150) {
                var self = this;
                window.setTimeout(function () {
                  self.tick();
                }, 2000);
              } else {
                this.detail = 'Sidan slutade vänta. Ladda om för att se läget.';
              }
            }
          } catch (_err) {
            if (this._tries < 150) {
              var self = this;
              window.setTimeout(function () {
                self.tick();
              }, 2000);
            }
          }
        },
      };
    });

    window.Alpine.data('researchPolling', function () {
      var ACTIVE_STATUSES = {
        queued: true,
        planning: true,
        searching: true,
        fetching: true,
        evaluating: true,
        synthesizing: true,
        running: true,
      };
      return {
        mounted: false,
        notMounted: true,
        status: 'queued',
        roundsCompleted: 0,
        queriesUsed: 0,
        fetchesUsed: 0,
        evidenceStatus: '',
        errorMessage: '',
        events: [],
        isActive: false,
        hasEvents: false,
        noEvents: true,
        hasError: false,
        hasEvidence: false,
        reloadReady: false,
        statusBadgeClass: 'az-badge az-badge-info',
        statusUrl: '',
        detailUrl: '',
        _wasActiveOnInit: false,
        _tries: 0,

        _sync: function () {
          this.notMounted = !this.mounted;
          this.isActive = Boolean(ACTIVE_STATUSES[this.status]);
          this.hasEvents = Array.isArray(this.events) && this.events.length > 0;
          this.noEvents = !this.hasEvents;
          this.hasError = Boolean(this.errorMessage);
          this.hasEvidence = Boolean(this.evidenceStatus);
          if (this.status === 'completed') {
            this.statusBadgeClass = 'az-badge az-badge-ok';
          } else if (this.status === 'partial' || this.status === 'cancelled') {
            this.statusBadgeClass = 'az-badge az-badge-warn';
          } else if (this.status === 'failed') {
            this.statusBadgeClass = 'az-badge az-badge-danger';
          } else {
            this.statusBadgeClass = 'az-badge az-badge-info';
          }
          if (this._wasActiveOnInit && !this.isActive) {
            this.reloadReady = true;
          }
        },

        _fmtUtcMinutes: function (raw) {
          if (!raw) return '';
          var s = String(raw).trim();
          if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/.test(s)) return s;
          var parsed = Date.parse(s);
          if (!isNaN(parsed)) {
            var d = new Date(parsed);
            var pad = function (n) { return n < 10 ? '0' + n : String(n); };
            return (
              d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) +
              ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes())
            );
          }
          return s.length >= 16 ? s.slice(0, 16).replace('T', ' ') : s;
        },

        _normalizeEvents: function (list) {
          var self = this;
          return (Array.isArray(list) ? list : []).map(function (ev, idx) {
            var rawTs = (ev && ev.created_at) ? String(ev.created_at) : '';
            var fmtTs = (ev && ev.created_at_fmt) ? String(ev.created_at_fmt) : self._fmtUtcMinutes(rawTs);
            return {
              seq: ev && ev.seq !== undefined ? ev.seq : idx + 1,
              phase: (ev && ev.phase) ? String(ev.phase) : 'info',
              message: (ev && ev.message) ? String(ev.message) : '',
              created_at: rawTs,
              created_at_fmt: fmtTs,
            };
          });
        },

        init: function () {
          var initScript = document.getElementById('research-events-init');
          if (initScript && initScript.textContent) {
            try {
              this.events = this._normalizeEvents(JSON.parse(initScript.textContent));
            } catch (_err) {
              this.events = [];
            }
          }
          if (this.$el) {
            this.statusUrl = this.$el.getAttribute('data-status-url') || '';
            this.detailUrl = this.$el.getAttribute('data-detail-url') || '';
            this.status = this.$el.getAttribute('data-initial-status') || 'queued';
            this.roundsCompleted = Number(this.$el.getAttribute('data-initial-rounds') || 0);
            this.queriesUsed = Number(this.$el.getAttribute('data-initial-queries') || 0);
            this.fetchesUsed = Number(this.$el.getAttribute('data-initial-fetches') || 0);
            this.evidenceStatus = this.$el.getAttribute('data-initial-evidence') || '';
            this.errorMessage = this.$el.getAttribute('data-initial-error') || '';
          }
          this._wasActiveOnInit = Boolean(ACTIVE_STATUSES[this.status]);
          this.mounted = true;
          this._sync();
          if (this.isActive && this.statusUrl) {
            var self = this;
            window.setTimeout(function () {
              self.tick();
            }, 2500);
          }
        },

        tick: async function () {
          if (!this.statusUrl) return;
          this._tries += 1;
          try {
            var res = await fetch(this.statusUrl, {
              credentials: 'same-origin',
              headers: { Accept: 'application/json' },
            });
            if (!res.ok) throw new Error('status');
            var body = await res.json();
            if (body && body.status) {
              var job = body.job && typeof body.job === 'object' ? body.job : {};
              this.status = String(body.status);
              if (body.rounds_completed !== undefined && body.rounds_completed !== null) {
                this.roundsCompleted = Number(body.rounds_completed);
              } else if (job.current_round !== undefined) {
                this.roundsCompleted = Number(job.current_round);
              }
              if (body.queries_used !== undefined && body.queries_used !== null) {
                this.queriesUsed = Number(body.queries_used);
              } else if (job.queries_used !== undefined) {
                this.queriesUsed = Number(job.queries_used);
              }
              if (body.fetches_used !== undefined && body.fetches_used !== null) {
                this.fetchesUsed = Number(body.fetches_used);
              } else if (job.fetches_used !== undefined) {
                this.fetchesUsed = Number(job.fetches_used);
              }
              var evStatus = body.evidence_status !== undefined ? body.evidence_status : job.evidence_status;
              if (evStatus) this.evidenceStatus = String(evStatus);
              var errMsg =
                (body.error_message !== undefined && body.error_message !== null && body.error_message !== '')
                  ? body.error_message
                  : ((job.error_message !== undefined && job.error_message !== null && job.error_message !== '')
                      ? job.error_message
                      : (body.error_code !== undefined ? body.error_code : job.error_class));
              if (errMsg) this.errorMessage = String(errMsg);
              if (Array.isArray(body.events)) {
                this.events = this._normalizeEvents(body.events);
              }
            }
            this._sync();
            if (this.isActive && this._tries < 240) {
              var self = this;
              window.setTimeout(function () {
                self.tick();
              }, 2500);
            }
          } catch (_err) {
            if (this.isActive && this._tries < 240) {
              var self = this;
              window.setTimeout(function () {
                self.tick();
              }, 2500);
            }
          }
        },
      };
    });
  }

  document.addEventListener('alpine:init', registerAlpineComponents);
  if (window.Alpine) registerAlpineComponents();

  /* ------------------------------------------------------------------ */
  /* 8. Research Hub Interactivity (Auto-Preview, Presets, Filter)      */
  /* ------------------------------------------------------------------ */
  function initResearchHub() {
    var hub = document.querySelector('[data-research-hub="true"]');
    if (!hub) return;

    var searchInput = document.getElementById('quick-search-query');
    var searchProfile = document.getElementById('quick-search-profile');
    var searchPreview = hub.querySelector('[data-auto-preview-search]');

    var deepInput = document.getElementById('deep-research-topic');
    var deepProfile = document.getElementById('deep-research-profile');
    var deepDepth = document.getElementById('deep-research-depth');
    var deepPreview = hub.querySelector('[data-auto-preview-deep]');

    var searchTimer = null;
    var deepTimer = null;
    var searchSeq = 0;
    var deepSeq = 0;

    function formatPreviewText(data) {
      if (!data || !data.profile) return '';
      var prof = data.profile;
      var title = String(prof.title || prof.id || '');
      var mode = String(prof.combination_mode || '');
      var fetchStrat = String(prof.fetch_strategy || '');
      var depth = String(data.resolved_depth || prof.ambition || '');
      var providers = Array.isArray(prof.preferred_providers)
        ? prof.preferred_providers.join(' -> ')
        : '';
      var parts = [];
      if (mode) parts.push(mode);
      if (fetchStrat) parts.push(fetchStrat);
      if (depth) parts.push('djup: ' + depth);
      if (providers) parts.push('motorer: ' + providers);
      return 'Automatiskt val: ' + title + (parts.length ? ' (' + parts.join(' · ') + ')' : '');
    }

    function fetchRecommend(text, ambition, previewEl, seqState, isSearch) {
      if (!previewEl) return;
      var trimmed = (text || '').trim();
      if (trimmed.length < 3) {
        previewEl.textContent = '';
        return;
      }
      var reqId = seqState + 1;
      if (isSearch) searchSeq = reqId;
      else deepSeq = reqId;

      var url =
        '/research/recommend?q=' +
        encodeURIComponent(trimmed) +
        '&ambition=' +
        encodeURIComponent(ambition || 'auto');

      fetch(url, {
        credentials: 'same-origin',
        headers: { Accept: 'application/json' },
      })
        .then(function (res) {
          if (!res.ok) throw new Error('recommend');
          return res.json();
        })
        .then(function (data) {
          if ((isSearch && reqId !== searchSeq) || (!isSearch && reqId !== deepSeq)) return;
          if (data && data.ok && data.profile) {
            previewEl.textContent = formatPreviewText(data);
          } else {
            previewEl.textContent = '';
          }
        })
        .catch(function () {
          if ((isSearch && reqId !== searchSeq) || (!isSearch && reqId !== deepSeq)) return;
          previewEl.textContent = '';
        });
    }

    function refreshSearchPreview() {
      if (!searchPreview || !searchInput || !searchProfile) return;
      var profVal = (searchProfile.value || '').trim();
      if (profVal && profVal !== 'auto') {
        searchPreview.textContent = '';
        return;
      }
      fetchRecommend(searchInput.value, 'auto', searchPreview, searchSeq, true);
    }

    function refreshDeepPreview() {
      if (!deepPreview || !deepInput || !deepProfile) return;
      var profVal = (deepProfile.value || '').trim();
      if (profVal && profVal !== 'auto') {
        deepPreview.textContent = '';
        return;
      }
      var ambition = deepDepth ? deepDepth.value : 'auto';
      fetchRecommend(deepInput.value, ambition || 'auto', deepPreview, deepSeq, false);
    }

    function scheduleSearchPreview() {
      if (searchTimer) window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(refreshSearchPreview, 250);
    }

    function scheduleDeepPreview() {
      if (deepTimer) window.clearTimeout(deepTimer);
      deepTimer = window.setTimeout(refreshDeepPreview, 250);
    }

    if (searchInput) searchInput.addEventListener('input', scheduleSearchPreview);
    if (searchProfile) searchProfile.addEventListener('change', refreshSearchPreview);
    if (deepInput) deepInput.addEventListener('input', scheduleDeepPreview);
    if (deepProfile) deepProfile.addEventListener('change', refreshDeepPreview);
    if (deepDepth) deepDepth.addEventListener('change', refreshDeepPreview);

    function selectHasOption(sel, val) {
      if (!sel || !val) return false;
      for (var i = 0; i < sel.options.length; i += 1) {
        if (sel.options[i].value === val) return true;
      }
      return false;
    }

    // 1-Click Snabbstarter
    hub.querySelectorAll('[data-research-preset]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var mode = btn.getAttribute('data-preset-mode') || 'search';
        var profId = btn.getAttribute('data-preset-profile') || 'auto';
        var depthVal = btn.getAttribute('data-preset-depth') || '';
        var placeholder = btn.getAttribute('data-preset-placeholder') || '';

        if (mode === 'search' && searchInput) {
          if (searchProfile && selectHasOption(searchProfile, profId)) {
            searchProfile.value = profId;
          }
          if (placeholder) searchInput.placeholder = placeholder;
          searchInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
          searchInput.focus();
          refreshSearchPreview();
        } else if (deepInput) {
          if (deepProfile && selectHasOption(deepProfile, profId)) {
            deepProfile.value = profId;
          }
          if (deepDepth && depthVal && selectHasOption(deepDepth, depthVal)) {
            deepDepth.value = depthVal;
          }
          if (placeholder) deepInput.placeholder = placeholder;
          deepInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
          deepInput.focus();
          refreshDeepPreview();
        }
      });
    });

    // Profile Catalog Filter
    var filterBtns = Array.prototype.slice.call(
      hub.querySelectorAll('[data-profile-ambition-filter]')
    );
    var profileCards = Array.prototype.slice.call(hub.querySelectorAll('[data-profile-card]'));
    filterBtns.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var targetAmb = btn.getAttribute('data-profile-ambition-filter') || 'all';
        filterBtns.forEach(function (other) {
          var active = other === btn;
          other.setAttribute('aria-pressed', active ? 'true' : 'false');
          other.classList.toggle('bg-ink-800', active);
          other.classList.toggle('ring-1', active);
          other.classList.toggle('ring-steel-500/35', active);
          other.classList.toggle('text-steel-200', active);
        });
        profileCards.forEach(function (card) {
          var cardAmb = card.getAttribute('data-ambition') || '';
          var show = targetAmb === 'all' || cardAmb === targetAmb;
          card.classList.toggle('hidden', !show);
        });
      });
    });

    // Profile Card Action Buttons
    hub.querySelectorAll('[data-apply-profile-search]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var profId = btn.getAttribute('data-apply-profile-search') || '';
        if (searchProfile && selectHasOption(searchProfile, profId)) {
          searchProfile.value = profId;
        }
        if (searchInput) {
          searchInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
          searchInput.focus();
        }
        refreshSearchPreview();
      });
    });

    hub.querySelectorAll('[data-apply-profile-deep]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var profId = btn.getAttribute('data-apply-profile-deep') || '';
        var amb = btn.getAttribute('data-profile-ambition') || '';
        if (deepProfile && selectHasOption(deepProfile, profId)) {
          deepProfile.value = profId;
        }
        if (deepDepth && (amb === 'quick' || amb === 'standard' || amb === 'deep') && selectHasOption(deepDepth, amb)) {
          deepDepth.value = amb;
        }
        if (deepInput) {
          deepInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
          deepInput.focus();
        }
        refreshDeepPreview();
      });
    });

    refreshSearchPreview();
    refreshDeepPreview();
  }

  /* ------------------------------------------------------------------ */
  /* 9. SEO Studio Progressive Enhancement (strict CSP, DOM-safe)       */
  /* ------------------------------------------------------------------ */
  function initSeoStudio() {
    var root = document.querySelector('[data-seo-studio="true"]');
    if (!root) return;

    function selectHasOptionValue(sel, val) {
      if (!sel) return false;
      for (var i = 0; i < sel.options.length; i += 1) {
        if (sel.options[i].value === val) return true;
      }
      return false;
    }

    // Quick-fill fix proposal form from an audit issue row
    root.querySelectorAll('[data-seo-fix-prefill]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var fixType = btn.getAttribute('data-fix-type') || 'seo_meta';
        var targetType = btn.getAttribute('data-target-type') || 'product';
        var targetId = btn.getAttribute('data-target-id') || '';
        var summary = btn.getAttribute('data-fix-summary') || '';
        var rawProposed = btn.getAttribute('data-proposed-json') || '';

        var fixTypeEl = document.getElementById('seo-fix-type');
        var targetTypeEl = document.getElementById('seo-fix-target-type');
        var targetIdEl = document.getElementById('seo-fix-target-id');
        var summaryEl = document.getElementById('seo-fix-summary');
        var metaTitleEl = document.getElementById('seo-fix-meta-title');
        var metaDescEl = document.getElementById('seo-fix-meta-desc');
        var focusEl = document.getElementById('seo-fix-focus');
        var altEl = document.getElementById('seo-fix-alt');
        var jsonEl = document.getElementById('seo-fix-proposed-json');

        if (fixTypeEl && selectHasOptionValue(fixTypeEl, fixType)) {
          fixTypeEl.value = fixType;
        }
        if (targetTypeEl && selectHasOptionValue(targetTypeEl, targetType)) {
          targetTypeEl.value = targetType;
        }
        if (targetIdEl) targetIdEl.value = targetId;
        if (summaryEl) summaryEl.value = summary;

        // Always reset stale field values before prefilling new issue
        var fieldMap = {
          meta_title: metaTitleEl,
          meta_description: metaDescEl,
          focus_keyphrase: focusEl,
          alt_text: altEl,
        };
        Object.keys(fieldMap).forEach(function (k) {
          if (fieldMap[k]) fieldMap[k].value = '';
        });
        if (jsonEl) jsonEl.value = '';

        if (rawProposed) {
          try {
            var parsed = JSON.parse(rawProposed);
            if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
              var hasExtraKeys = false;
              Object.keys(parsed).forEach(function (k) {
                var val = parsed[k];
                if (val === null || val === undefined) return;
                if (Object.prototype.hasOwnProperty.call(fieldMap, k) && fieldMap[k]) {
                  fieldMap[k].value = String(val);
                } else {
                  hasExtraKeys = true;
                }
              });
              if (hasExtraKeys && jsonEl) {
                jsonEl.value = JSON.stringify(parsed, null, 2);
                var details = jsonEl.closest('details');
                if (details) details.open = true;
              }
            }
          } catch (_err) {}
        }

        [metaTitleEl, metaDescEl].forEach(function (el) {
          if (el) el.dispatchEvent(new Event('input', { bubbles: true }));
        });

        var formSection = document.getElementById('seo-fix-propose-section');
        if (formSection) {
          formSection.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
        if (targetIdEl) targetIdEl.focus();
      });
    });

    // Live Google SERP Desktop/Mobile preview & char meters (<= 60 title / <= 158 desc)
    var serpBox = root.querySelector('[data-serp-preview="1"]');
    if (serpBox) {
      var serpCard = document.getElementById('seo-serp-card');
      var serpTitleEl = document.getElementById('seo-serp-title');
      var serpDescEl = document.getElementById('seo-serp-desc');
      var serpTitleMeter = document.getElementById('seo-serp-title-meter');
      var serpDescMeter = document.getElementById('seo-serp-desc-meter');
      var titleInput = document.getElementById('seo-fix-meta-title');
      var descInput = document.getElementById('seo-fix-meta-desc');
      var modeBtns = Array.prototype.slice.call(serpBox.querySelectorAll('[data-serp-mode]'));
      var serpMode = 'desktop';

      var renderSerpPreview = function () {
        var titleLimit = serpMode === 'mobile' ? 55 : 60;
        var descLimit = serpMode === 'mobile' ? 120 : 158;
        var tVal = titleInput ? (titleInput.value || '').trim() : '';
        var dVal = descInput ? (descInput.value || '').trim() : '';

        if (serpTitleEl) {
          serpTitleEl.textContent = tVal || 'Skriv en meta-titel för att förhandsvisa Google-resultatet';
        }
        if (serpDescEl) {
          serpDescEl.textContent =
            dVal || 'Skriv en meta-beskrivning (rekommenderat upp till 158 tecken) för att se utdraget i sökresultatet.';
        }
        if (serpTitleMeter) {
          var tLen = tVal.length;
          serpTitleMeter.textContent =
            'Titel: ' + tLen + ' / 60 tecken' + (tLen > 60 ? ' (för lång)' : (tLen > titleLimit ? ' (mobil trunkering)' : ' (OK)'));
          serpTitleMeter.classList.toggle('text-danger-text', tLen > 60);
          serpTitleMeter.classList.toggle('text-warn', tLen > titleLimit && tLen <= 60);
          serpTitleMeter.classList.toggle('text-mist-400', tLen <= titleLimit);
        }
        if (serpDescMeter) {
          var dLen = dVal.length;
          serpDescMeter.textContent =
            'Beskrivning: ' + dLen + ' / 158 tecken' + (dLen > 158 ? ' (för lång)' : (dLen > descLimit ? ' (mobil trunkering)' : ' (OK)'));
          serpDescMeter.classList.toggle('text-danger-text', dLen > 158);
          serpDescMeter.classList.toggle('text-warn', dLen > descLimit && dLen <= 158);
          serpDescMeter.classList.toggle('text-mist-400', dLen <= descLimit);
        }
        if (serpCard) {
          serpCard.classList.toggle('max-w-md', serpMode === 'mobile');
        }
      };

      modeBtns.forEach(function (btn) {
        btn.addEventListener('click', function () {
          serpMode = btn.getAttribute('data-serp-mode') || 'desktop';
          modeBtns.forEach(function (other) {
            var active = other === btn;
            other.setAttribute('aria-pressed', active ? 'true' : 'false');
            other.classList.toggle('bg-ink-800', active);
            other.classList.toggle('ring-1', active);
            other.classList.toggle('ring-steel-500/35', active);
            other.classList.toggle('text-steel-200', active);
          });
          renderSerpPreview();
        });
      });

      if (titleInput) titleInput.addEventListener('input', renderSerpPreview);
      if (descInput) descInput.addEventListener('input', renderSerpPreview);
      renderSerpPreview();
    }

    // Interactive Audit Issue Filter Pills, Live Text Search & "Nästa olösta fynd"
    var issueRows = Array.prototype.slice.call(root.querySelectorAll('[data-seo-issue-row="1"]'));
    var issueFilterBtns = Array.prototype.slice.call(root.querySelectorAll('[data-seo-issue-filter]'));
    var issueSearchInput = document.getElementById('seo-issue-search');
    var issueCountEl = document.getElementById('seo-issue-count');
    var nextIssueBtn = root.querySelector('[data-seo-next-issue="1"]');
    if (issueRows.length) {
      var activeFilter = 'all';
      var nextCursor = -1;

      var applyIssueFilters = function () {
        var q = issueSearchInput ? (issueSearchInput.value || '').trim().toLowerCase() : '';
        var visibleCount = 0;
        issueRows.forEach(function (row) {
          var sev = (row.getAttribute('data-severity') || '').toLowerCase();
          var cat = (row.getAttribute('data-category') || '').toLowerCase();
          var txt = (row.getAttribute('data-issue-text') || '').toLowerCase();
          var matchesPill =
            activeFilter === 'all' ||
            sev === activeFilter ||
            cat === activeFilter ||
            (activeFilter === 'on_page' && (cat === 'on_page' || cat === 'on-page' || cat === 'content' || cat === 'meta'));
          var matchesSearch = !q || txt.indexOf(q) !== -1;
          var show = matchesPill && matchesSearch;
          row.classList.toggle('hidden', !show);
          if (show) visibleCount += 1;
        });
        if (issueCountEl) issueCountEl.textContent = String(visibleCount);
        nextCursor = -1;
      };

      issueFilterBtns.forEach(function (btn) {
        btn.addEventListener('click', function () {
          activeFilter = (btn.getAttribute('data-seo-issue-filter') || 'all').toLowerCase();
          issueFilterBtns.forEach(function (other) {
            var active = other === btn;
            other.setAttribute('aria-pressed', active ? 'true' : 'false');
            other.classList.toggle('bg-ink-800', active);
            other.classList.toggle('ring-1', active);
            other.classList.toggle('ring-steel-500/35', active);
            other.classList.toggle('text-steel-200', active);
          });
          applyIssueFilters();
        });
      });

      if (issueSearchInput) {
        issueSearchInput.addEventListener('input', applyIssueFilters);
      }

      if (nextIssueBtn) {
        nextIssueBtn.addEventListener('click', function () {
          var visible = issueRows.filter(function (r) {
            return !r.classList.contains('hidden');
          });
          if (!visible.length) return;
          nextCursor = (nextCursor + 1) % visible.length;
          var targetRow = visible[nextCursor];
          issueRows.forEach(function (r) {
            r.classList.remove('bg-ink-800');
          });
          targetRow.classList.add('bg-ink-800');
          var prefillBtn = targetRow.querySelector('[data-seo-fix-prefill]');
          if (prefillBtn) {
            prefillBtn.click();
          } else {
            targetRow.scrollIntoView({ behavior: 'smooth', block: 'center' });
          }
        });
      }
    }

    // Live anchor-mix hint on Link Package builder textarea (mirrors skills/ecom_ops/seo/policy.py)
    var targetsInput = document.getElementById('seo-link-targets-raw');
    var notesInput = document.getElementById('seo-lp-notes');
    var mixHint = document.getElementById('seo-link-mix-preview');
    if (targetsInput && mixHint) {
      var spamPbnRe = /\b(?:pbn|private\s+blog\s+network|link\s*farm|l[aä]nkfarm|link\s*wheel|comment\s+spam|forum\s+spam|xrumer|gsa\s+ser|cloaking|doorway\s+pages?|hidden\s+links?|casino|gambling|slots|poker|payday\s+loan|viagra|cialis|porn|adult\s+links?|\d{3,}\s+(?:backlinks?|links?)\s+blast|cheap\s+bulk\s+links?)\b/i;
      var genericAnchors = {
        'klicka här': 1, 'läs mer': 1, 'läs mer här': 1, 'här': 1, 'se mer': 1,
        'se utbudet': 1, 'se produkter': 1, 'besök hemsidan': 1, 'denna guide': 1,
        'denna sida': 1, 'länk': 1, 'hemsida': 1, 'köp här': 1,
        'les mer': 1, 'les mer her': 1, 'klikk her': 1, 'her': 1, 'se utvalget': 1,
        'besøk nettsiden': 1, 'denne siden': 1, 'læs mere': 1, 'læs mere her': 1,
        'klik her': 1, 'se udvalget': 1, 'besøg hjemmesiden': 1, 'click here': 1,
        'read more': 1, 'here': 1, 'visit site': 1, 'learn more': 1, 'this page': 1,
        'website': 1, 'link': 1, 'more info': 1,
      };
      var brandTerms = {
        'azom': 1, 'azom.no': 1, 'azom.se': 1, 'azom.dk': 1,
        'azom nordic': 1, 'azom bilstereo': 1, 'azom butik': 1,
        'azom nettbutikk': 1, 'azom shop': 1,
      };
      var normTokens = function (text) {
        var matches = String(text || '').toLowerCase().match(/[a-z0-9åäöøæ]{2,}/g);
        return matches || [];
      };
      var tokenSetEqual = function (a, b) {
        if (!a.length || !b.length) return false;
        var mapA = {};
        var mapB = {};
        a.forEach(function (t) { mapA[t] = 1; });
        b.forEach(function (t) { mapB[t] = 1; });
        var keysA = Object.keys(mapA);
        var keysB = Object.keys(mapB);
        if (keysA.length !== keysB.length) return false;
        return keysA.every(function (k) { return !!mapB[k]; });
      };
      var inferAnchorType = function (url, kw, anchor) {
        var low = String(anchor || '').trim().replace(/\s+/g, ' ').toLowerCase().replace(/^[ .,:;!?→»]+|[ .,:;!?→»]+$/g, '');
        if (!low) return 'generic';
        if (/^(?:https?:\/\/|www\.)/i.test(low) || /^[a-z0-9-]+\.(?:no|se|dk|com|net|org)(?:\/[^\s]*)?$/i.test(low)) {
          return 'naked_url';
        }
        if (Object.prototype.hasOwnProperty.call(genericAnchors, low)) return 'generic';
        var kwClean = String(kw || '').trim().replace(/\s+/g, ' ').toLowerCase();
        var kwTokens = normTokens(kwClean);
        var anchorTokens = normTokens(low);
        if (kwClean) {
          if (low === kwClean || tokenSetEqual(anchorTokens, kwTokens)) return 'exact';
          if (kwTokens.length) {
            var kwMap = {};
            kwTokens.forEach(function (t) { kwMap[t] = 1; });
            if (anchorTokens.some(function (t) { return !!kwMap[t]; })) return 'partial';
          }
        }
        if (Object.prototype.hasOwnProperty.call(brandTerms, low)) return 'brand';
        if (anchorTokens.indexOf('azom') !== -1 && anchorTokens.length <= 2) return 'brand';
        return 'partial';
      };

      var updateMixHint = function () {
        var lines = (targetsInput.value || '')
          .split(/\r?\n/)
          .map(function (l) {
            return l.trim();
          })
          .filter(Boolean);
        var notesText = notesInput ? (notesInput.value || '') : '';
        var notesSpam = spamPbnRe.test(notesText);
        if (!lines.length) {
          if (notesSpam) {
            mixHint.textContent = 'BLOCKERAS (anti-spam/PBN-termer upptäckta i anteckningar)';
            return;
          }
          mixHint.textContent =
            'Lämna tomt för automatisk naturlig ankarprofil (60%+ varumärke/URL/generisk, <=10% exact).';
          return;
        }
        var counts = { brand: 0, naked_url: 0, generic: 0, partial: 0, exact: 0 };
        var total = 0;
        var spamHit = notesSpam;
        lines.forEach(function (line) {
          var cols = line.split('|').map(function (c) {
            return c.trim();
          });
          var url = cols[0] || '';
          var kw = cols[1] || '';
          var explicitType = (cols[2] || '').toLowerCase();
          var anchor = cols[3] || '';
          if (spamPbnRe.test(anchor + ' ' + kw + ' ' + url)) {
            spamHit = true;
          }
          if (explicitType === 'partial_match') explicitType = 'partial';
          if (explicitType === 'exact_match') explicitType = 'exact';
          if (explicitType === 'url' || explicitType === 'naked') explicitType = 'naked_url';

          var inferred = anchor ? inferAnchorType(url, kw, anchor) : '';
          var t = explicitType;
          if (!t) {
            t = inferred || 'brand';
          } else if (inferred && (inferred === 'exact' || inferred === 'partial') && t !== inferred) {
            t = inferred;
          }
          if (Object.prototype.hasOwnProperty.call(counts, t)) counts[t] += 1;
          else counts.brand += 1;
          total += 1;
        });
        var exactPct = Math.round((counts.exact / total) * 100);
        var partialPct = Math.round((counts.partial / total) * 100);
        var naturalPct = Math.round(((counts.brand + counts.naked_url + counts.generic) / total) * 100);
        var status = '';
        if (spamHit) {
          status = 'BLOCKERAS (otillåtna anti-spam/PBN-termer upptäckta)';
        } else if (exactPct > 20 || naturalPct < 35) {
          status = 'BLOCKERAS (för hög exact-match > 20% eller låg naturlig andel < 35%)';
        } else if (exactPct > 10 || naturalPct < 60 || partialPct > 25) {
          var reasons = [];
          if (exactPct > 10) reasons.push('exact-match > 10%');
          if (naturalPct < 60) reasons.push('naturlig andel < 60%');
          if (partialPct > 25) reasons.push('partial-match > 25%');
          status = 'VARNING (' + reasons.join(', ') + ')';
        } else {
          status = 'OK (naturlig ankarprofil)';
        }
        mixHint.textContent =
          total + ' mål-rader · Naturlig andel: ' + naturalPct + '% · Exact: ' + exactPct + '% — ' + status;
      };
      targetsInput.addEventListener('input', updateMixHint);
      if (notesInput) notesInput.addEventListener('input', updateMixHint);
      updateMixHint();
    }
  }

  function boot() {
    initDrawer();
    initDialogLightDismiss();
    initCommandPalette();
    initConfirmAndBusy();
    initUtilityHooks();
    initVanillaXaiPolling();
    initResearchHub();
    initSeoStudio();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
