// Desplegable de la campana de avisos.
//
// Sin este fichero la campana sigue funcionando: es el boton de un formulario
// que hace POST a /atomicdb/notifications/, marca los avisos como vistos y
// aterriza en la pagina con la lista entera.  Lo unico que anade esto es no
// tener que salir de la pagina: intercepta el envio, abre el panel que ya
// vino renderizado con la respuesta, y manda el MISMO POST de fondo.
//
// La lista no se pide aqui.  Llega pintada desde el servidor con la pagina,
// asi que no hay sondeo, ni temporizadores, ni un estado del cliente que
// pueda discrepar del servidor.
(function () {
  'use strict';

  const wrap = document.getElementById('bell-wrap');
  const bell = document.getElementById('bell');
  const panel = document.getElementById('bell-panel');
  if (!wrap || !bell || !panel) return;

  const form = bell.closest('form');
  let marked = false;

  function open() {
    return !panel.hidden;
  }

  function show(visible) {
    panel.hidden = !visible;
    bell.setAttribute('aria-expanded', visible ? 'true' : 'false');
  }

  // La insignia y el nombre accesible del boton dicen lo mismo, asi que se
  // apagan juntos: dejar el "4 new" del lector de pantalla despues de abrir
  // el panel seria contarle a alguien un estado que ya no existe.
  function clearBadge() {
    const badge = bell.querySelector('.bell-badge');
    if (badge) badge.remove();
    const label = bell.querySelector('.visually-hidden');
    if (label && label.dataset.quiet) label.textContent = label.dataset.quiet;
  }

  // El marcado es del servidor; esto solo se lo pide sin recargar. Falla en
  // silencio a proposito: si la peticion no sale, los avisos siguen sin ver y
  // la siguiente carga de pagina vuelve a ensenar la insignia, que es la
  // verdad.
  function markSeen() {
    if (marked || !form || !window.fetch) return;
    marked = true;
    const body = new FormData(form);
    body.delete('back');
    fetch(form.action, {
      method: 'POST',
      body: body,
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'fetch' },
    }).then(function (response) {
      if (response.ok) clearBadge();
      else marked = false;
    }).catch(function () {
      marked = false;
    });
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    const opening = !open();
    show(opening);
    if (opening) markSeen();
  });

  document.addEventListener('click', function (event) {
    if (open() && !wrap.contains(event.target)) show(false);
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && open()) {
      show(false);
      bell.focus();
    }
  });
}());
