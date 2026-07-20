










namespace SaltyPotatoAI.Browser;

internal static class PageBridge
{
    public const string Script = """
(() => {
  if (window.__salty) { return; }

  const registry = new Map();
  const handles = new WeakMap();
  let sequence = 0;



  const accessibleName = (node) => {
    const aria = node.getAttribute?.('aria-label');
    if (aria) return aria.trim();
    const labelledBy = node.getAttribute?.('aria-labelledby');
    if (labelledBy) {
      const parts = labelledBy.split(/\s+/)
        .map((id) => document.getElementById(id))
        .filter(Boolean)
        .map((item) => (item.innerText || item.textContent || '').trim());
      if (parts.length) return parts.join(' ').trim();
    }
    if (node.labels && node.labels.length) {
      return (node.labels[0].innerText || '').trim();
    }
    const title = node.getAttribute?.('title');
    if (title) return title.trim();
    const alt = node.getAttribute?.('alt');
    if (alt) return alt.trim();
    const placeholder = node.getAttribute?.('placeholder');
    if (placeholder) return placeholder.trim();
    const text = (node.innerText || node.textContent || '').trim();
    return text.length > 160 ? text.slice(0, 160) : text;
  };

  const role = (node) => {
    const explicit = node.getAttribute?.('role');
    if (explicit) return explicit;
    const tag = node.tagName.toLowerCase();
    if (tag === 'a') return node.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const type = (node.getAttribute('type') || 'text').toLowerCase();
      if (type === 'checkbox') return 'checkbox';
      if (type === 'radio') return 'radio';
      if (['button', 'submit', 'reset', 'image'].includes(type)) return 'button';
      return 'textbox';
    }
    if (/^h[1-6]$/.test(tag)) return 'heading';
    return tag;
  };

  const visible = (node) => {
    const rect = node.getBoundingClientRect?.();
    if (!rect || (rect.width === 0 && rect.height === 0)) return false;
    const style = window.getComputedStyle(node);
    return style.visibility !== 'hidden' && style.display !== 'none'
      && Number(style.opacity) !== 0;
  };

  const editable = (node) => {
    const tag = node.tagName.toLowerCase();
    if (node.isContentEditable) return true;
    if (tag === 'textarea') return !node.disabled && !node.readOnly;
    if (tag !== 'input') return false;
    const type = (node.getAttribute('type') || 'text').toLowerCase();
    const typeable = ['text', 'search', 'email', 'url', 'tel', 'password', 'number'];
    return typeable.includes(type) && !node.disabled && !node.readOnly;
  };

  const describe = (node, handle) => {
    const rect = node.getBoundingClientRect?.() || { x: 0, y: 0, width: 0, height: 0 };
    const info = {
      element: handle,
      tag: node.tagName.toLowerCase(),
      role: role(node),
      name: accessibleName(node),
      visible: visible(node),
      enabled: !node.disabled,
      editable: editable(node),
      bounds: {
        x: Math.round(rect.x), y: Math.round(rect.y),
        width: Math.round(rect.width), height: Math.round(rect.height)
      }
    };
    if (node.id) info.id = node.id;
    if (node.value !== undefined && node.type !== 'password') info.value = String(node.value).slice(0, 400);
    if (node.href) info.href = node.href;
    if (node.checked !== undefined) info.checked = !!node.checked;
    if (node.selected !== undefined) info.selected = !!node.selected;
    const text = (node.innerText || '').trim();
    if (text && text !== info.name) info.text = text.slice(0, 200);
    return info;
  };

  const register = (node) => {
    const existing = handles.get(node);
    if (existing && registry.get(existing) === node && node.isConnected) return existing;
    const handle = 'web-el-' + (++sequence);
    registry.set(handle, node);
    handles.set(node, handle);
    return handle;
  };




  const resolve = (handle) => {
    const node = registry.get(handle);
    if (!node) throw { kind: 'stale_element', message: 'Unknown element ' + handle };
    if (!node.isConnected) {
      registry.delete(handle);
      handles.delete(node);
      throw { kind: 'stale_element', message: 'Element ' + handle + ' left the page' };
    }
    return node;
  };

  const INTERESTING = 'a,button,input,select,textarea,[role],[onclick],[contenteditable],summary,h1,h2,h3';

  const describeMedia = (node) => {
    const duration = Number(node.duration);
    const error = node.error;
    return {
      element: register(node),
      kind: node.tagName.toLowerCase(),
      name: accessibleName(node) || document.title,
      visible: visible(node),
      src: String(node.currentSrc || node.src || '').slice(0, 2000),
      paused: !!node.paused,
      ended: !!node.ended,
      playing: !node.paused && !node.ended && node.readyState >= 2,
      current_time: Number(node.currentTime || 0),
      duration: Number.isFinite(duration) ? duration : null,
      ready_state: Number(node.readyState || 0),
      network_state: Number(node.networkState || 0),
      muted: !!node.muted,
      volume: Number(node.volume ?? 1),
      error: error ? { code: Number(error.code || 0), message: String(error.message || '') } : null
    };
  };

  const mediaNodes = () => Array.from(document.querySelectorAll('video,audio'));

  window.__salty = {
    page: () => ({
      url: location.href,
      title: document.title,
      ready: document.readyState
    }),

    query: (options) => {
      const opts = options || {};
      const limit = Math.min(Math.max(opts.limit || 20, 1), 100);
      const wanted = (opts.name || '').toLowerCase();
      const wantedText = (opts.text || '').toLowerCase();
      const wantedHref = (opts.href || '').toLowerCase();
      const candidates = Array.from(document.querySelectorAll(opts.selector || INTERESTING));
      const matches = [];
      for (const node of candidates) {
        if (opts.role && role(node) !== opts.role) continue;
        if (opts.editable === true && !editable(node)) continue;
        if (opts.visible !== false && !visible(node)) continue;
        if (opts.enabled === true && node.disabled) continue;
        if (wanted) {
          const name = accessibleName(node).toLowerCase();
          if (opts.exact ? name !== wanted : !name.includes(wanted)) continue;
        }
        if (wantedText && !(node.innerText || '').toLowerCase().includes(wantedText)) continue;
        if (wantedHref && !String(node.href || '').toLowerCase().includes(wantedHref)) continue;
        matches.push(describe(node, register(node)));
        if (matches.length >= limit) break;
      }
      return { matches, count: matches.length, ambiguous: matches.length > 1 };
    },

    read: (options) => {
      const opts = options || {};
      const limit = Math.min(Math.max(opts.limit || 60, 1), 200);
      const nodes = Array.from(document.querySelectorAll(INTERESTING))
        .filter(visible)
        .slice(0, limit);
      return {
        url: location.href,
        title: document.title,
        heading: (document.querySelector('h1')?.innerText || '').trim().slice(0, 200),


        summary: (document.body?.innerText || '').trim().slice(0, opts.text_limit || 2000),
        controls: nodes.map((node) => describe(node, register(node))),
        control_count: nodes.length
      };
    },

    get: (handle) => describe(resolve(handle), handle),

    media: () => {
      const media = mediaNodes().map(describeMedia);
      return { media, count: media.length, url: location.href, title: document.title };
    },

    mediaAct: (handle, action) => {
      let node;
      if (handle) {
        node = resolve(handle);
        if (!['video', 'audio'].includes(node.tagName.toLowerCase())) {
          throw { kind: 'invalid_target', message: 'That element is not media' };
        }
      } else {
        const candidates = mediaNodes().filter(visible);
        if (!candidates.length) throw { kind: 'not_found', message: 'No visible media exists' };
        if (candidates.length > 1) {
          throw { kind: 'ambiguous', message: 'More than one visible media element exists' };
        }
        node = candidates[0];
      }
      if (action === 'play') {
        const attempt = node.play();
        if (attempt?.catch) attempt.catch(() => {});
      } else if (action === 'pause') {
        node.pause();
      } else {
        throw { kind: 'invalid_request', message: 'Unknown media action ' + action };
      }
      return describeMedia(node);
    },

    act: (handle, action, payload) => {
      const node = resolve(handle);
      const data = payload || {};
      if (action === 'click') {
        node.scrollIntoView({ block: 'center' });
        node.click();
      } else if (action === 'set_value') {
        if (!editable(node) && node.tagName.toLowerCase() !== 'select') {
          throw { kind: 'not_editable', message: 'That element cannot be typed into' };
        }
        node.focus();
        const setter = Object.getOwnPropertyDescriptor(
          node.tagName.toLowerCase() === 'textarea'
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype,
          'value'
        )?.set;


        if (setter) { setter.call(node, String(data.value ?? '')); }
        else { node.value = String(data.value ?? ''); }
        node.dispatchEvent(new Event('input', { bubbles: true }));
        node.dispatchEvent(new Event('change', { bubbles: true }));
      } else if (action === 'select') {
        node.focus();
        node.value = String(data.value ?? '');
        node.dispatchEvent(new Event('change', { bubbles: true }));
      } else if (action === 'focus') {
        node.focus();
      } else if (action === 'scroll') {
        node.scrollIntoView({ block: 'center' });
      } else if (action === 'submit') {
        const form = node.form || node.closest('form');
        if (!form) throw { kind: 'no_form', message: 'That element is not inside a form' };
        form.submit();
      } else {
        throw { kind: 'invalid_request', message: 'Unknown action ' + action };
      }
      return describe(node, handle);
    }
  };
})();
""";





    public static string Call(string expression) =>
        "(() => { try { return JSON.stringify({ ok: true, value: " + expression + " }); }"
        + " catch (error) { return JSON.stringify({ ok: false, error: "
        + "{ kind: error?.kind || 'failed', message: String(error?.message || error) } }); } })()";
}
