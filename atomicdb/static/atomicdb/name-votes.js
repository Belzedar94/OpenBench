// Opening-name votes on the Openings page (/atomicdb/roots/).
//
// The page is cached for everybody, so its numbers can be a little old.  On
// load this reads the live tallies and the visitor's own votes from
// /atomicdb/names/votes/ and paints them; a click sends the vote with fetch
// and paints the answer.  Without this script the buttons are plain forms
// and the server sends the browser back to the page.
(function () {
  'use strict';
  var table = document.querySelector('.namevote-table');
  if (!table || !window.fetch) return;

  function paint(form, up, down, mine) {
    var buttons = form.querySelectorAll('.namevote-btn');
    for (var i = 0; i < buttons.length; i++) {
      var b = buttons[i];
      var n = b.querySelector('.namevote-n');
      if (n) n.textContent = b.value === 'up' ? up : down;
      var on = mine === b.value;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.classList.toggle('is-mine', on);
    }
  }

  function markReview(form, pollUrl) {
    var row = form.closest('tr');
    var flag = row && row.querySelector('.namevote-flag');
    if (!flag) return;
    flag.hidden = false;
    if (pollUrl && !flag.querySelector('a')) {
      flag.appendChild(document.createTextNode(' · '));
      var a = document.createElement('a');
      a.href = pollUrl;
      a.rel = 'noopener';
      a.textContent = 'Discord poll';
      flag.appendChild(a);
    }
  }

  fetch(table.getAttribute('data-votes-url'), {
    credentials: 'same-origin', headers: {'Accept': 'application/json'}
  }).then(function (r) { return r.ok ? r.json() : null; }).then(function (data) {
    if (!data) return;
    var forms = table.querySelectorAll('form.namevote');
    for (var i = 0; i < forms.length; i++) {
      var key = forms[i].getAttribute('data-key');
      var t = data.votes[key] || [0, 0];
      paint(forms[i], t[0], t[1], data.mine[key] || null);
      if (Object.prototype.hasOwnProperty.call(data.under_review, key)) {
        markReview(forms[i], data.under_review[key]);
      }
    }
  }).catch(function () {});

  table.addEventListener('click', function (event) {
    var button = event.target.closest('.namevote-btn');
    if (!button) return;
    var form = button.form;
    event.preventDefault();
    var body = new FormData(form);
    body.set('vote', button.value);
    button.disabled = true;
    fetch(form.action, {
      method: 'POST', body: body, credentials: 'same-origin',
      headers: {'X-Requested-With': 'fetch', 'Accept': 'application/json'}
    }).then(function (r) { return r.json(); }).then(function (data) {
      paint(form, data.up, data.down, data.mine);
      if (data.status === 'rate-limited') {
        button.title = 'Too many votes from your network on this name';
      }
      if (data.under_review) markReview(form, '');
    }).catch(function () {
      form.submit();
    }).then(function () { button.disabled = false; });
  });
})();
