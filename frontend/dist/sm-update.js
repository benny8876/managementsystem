/* ⬆ App update card for hospital / clinic site servers.
   Shown only to admin accounts, only on a site server (the cloud answers site:false),
   only when a newer version has been downloaded in the background.
   Backend: /api/v1/site-update/status and /apply (backend/app/api/v1/site_update.py). */
(function () {
  var ADMIN = ['super_admin', 'hospital_admin', 'branch_admin'];
  var box = null, busy = false, later = '';

  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }
  function session() { try { return JSON.parse(get('session') || 'null'); } catch (e) { return null; } }
  function auth() { var t = get('token'); return t ? { Authorization: 'Bearer ' + t } : null; }
  function el(tag, css, text) { var x = document.createElement(tag); if (css) x.style.cssText = css; if (text != null) x.textContent = text; return x; }

  function hide() { if (box) { box.remove(); box = null; } }
  function card(color) {
    hide();
    box = el('div', 'position:fixed;left:16px;bottom:16px;z-index:9999;max-width:340px;background:#fff;border:1px solid ' + color +
      ';border-left:5px solid ' + color + ';border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.18);padding:12px 14px;font-family:inherit;font-size:14px;color:#0f172a');
    document.body.appendChild(box);
    return box;
  }
  function btn(text, primary) {
    return el('button', 'border-radius:8px;padding:6px 12px;font-weight:600;cursor:pointer;border:1px solid ' +
      (primary ? '#15803d;background:#15803d;color:#fff' : '#cbd5e1;background:#fff;color:#334155'), text);
  }

  function showReady(s) {
    if (later === s.ready.version) return;
    var b = card('#15803d');
    b.appendChild(el('div', 'font-weight:700;font-size:15px', '⬆ Version အသစ် ရှိပါတယ်'));
    b.appendChild(el('div', 'color:#475569;margin:4px 0 10px;font-size:12px', s.ready.version + '  (အခု ' + (s.current || '—') + ')'));
    if (!s.can_apply) { b.appendChild(el('div', 'font-size:12px;color:#64748b', 'Admin account နဲ့ ဝင်ပြီး update လုပ်ပါ')); return; }
    var row = el('div', 'display:flex;gap:8px');
    var go = btn('Update လုပ်မယ်', true), no = btn('နောက်မှ', false);
    go.onclick = function () { apply(s.ready.version); };
    no.onclick = function () { later = s.ready.version; hide(); };
    row.appendChild(go); row.appendChild(no); b.appendChild(row);
  }

  function showMsg(color, title, text, keep) {
    var b = card(color);
    b.appendChild(el('div', 'font-weight:700;font-size:15px', title));
    if (text) b.appendChild(el('div', 'color:#475569;margin-top:4px;font-size:12px;white-space:pre-line', text));
    if (!keep) { var x = btn('OK', false); x.style.marginTop = '8px'; x.onclick = hide; b.appendChild(x); }
  }

  function apply(version) {
    if (!confirm('⬆ ' + version + ' ကို update လုပ်မယ်။\n\nUpdate လုပ်နေစဉ် ၁–၂ မိနစ်လောက် system ရပ်ပါမယ် (computer အားလုံး)။\nBill / ငွေရှင်း မလုပ်နေတဲ့အချိန် နှိပ်ပါ။\nData တွေ မပျက်ပါ — အရင် backup ယူပါမယ်။\n\nဆက်လုပ်မလား?')) return;
    var h = auth(); if (!h) return;
    busy = true;
    showMsg('#b45309', '⏳ Update စနေပါတယ်…', 'Page ကို မပိတ်ပါနဲ့။ ပြီးရင် အလိုလို ပြန်ဖွင့်ပါမယ်။', true);
    fetch('/api/v1/site-update/apply', { method: 'POST', headers: h }).then(function (r) {
      return r.json().then(function (d) { if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status)); return d; });
    }).then(function () { wait(version, Date.now(), false); })
      .catch(function (e) { busy = false; showMsg('#dc2626', '⚠ Update မစနိုင်ပါ', String(e.message || e)); });
  }

  function wait(version, t0, wentDown) {
    if (Date.now() - t0 > 6 * 60 * 1000) { busy = false; showMsg('#dc2626', '⚠ Update ကြာနေပါတယ်', 'Page ကို refresh လုပ်ကြည့်ပါ။'); return; }
    fetch('/health', { cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw 0;
      if (!wentDown && Date.now() - t0 < 15000) { setTimeout(function () { wait(version, t0, false); }, 2500); return; }
      return fetch('/api/v1/site-update/status', { headers: auth() || {} }).then(function (r) { return r.json(); }).then(function (s) {
        if (s.current === version) {
          showMsg('#15803d', '✅ Update ပြီးပါပြီ', version, true);
          setTimeout(function () { location.reload(); }, 2000);
        } else if (s.last && s.last.version === version && s.last.ok === false) {
          busy = false; showMsg('#dc2626', '⚠ Update မအောင်မြင်ပါ', s.last.message + '\nVersion အဟောင်းနဲ့ ဆက်သုံးနေပါတယ်။');
        } else setTimeout(function () { wait(version, t0, wentDown); }, 3000);
      });
    }).catch(function () { setTimeout(function () { wait(version, t0, true); }, 3000); });
  }

  function check() {
    if (busy) return;
    var s0 = session(), h = auth();
    if (!s0 || !h || ADMIN.indexOf(s0.role) < 0) { hide(); return; }
    fetch('/api/v1/site-update/status', { headers: h }).then(function (r) { return r.ok ? r.json() : null; }).then(function (s) {
      if (!s || !s.site) { hide(); return; }
      if (s.ready) { showReady(s); return; }
      if (s.last && s.last.ok === false && get('smUpdFail') !== s.last.version + s.last.at) {
        set('smUpdFail', s.last.version + s.last.at);
        showMsg('#dc2626', '⚠ နောက်ဆုံး update မအောင်မြင်ပါ', s.last.message);
        return;
      }
      if (box && !busy) hide();
    }).catch(function () {});
  }

  window.addEventListener('load', function () { setTimeout(check, 3000); });
  setInterval(check, 60 * 1000);
  window.addEventListener('storage', function (e) { if (e.key === 'session' || e.key === 'token') check(); });
})();
