// Desplegable de la campana de avisos.
//
// Sin este fichero la campana sigue funcionando: es un ENLACE a la pagina de
// avisos, que no marca nada por si misma.  Lo que anade esto es no salir de
// la pagina: intercepta el click, abre el panel que ya vino renderizado, y
// deja el enlace como fallback para quien navega sin JS.
//
// LEIDA = VISITADA.  Abrir el panel no marca nada — lo que apaga un aviso es
// llegar a su posicion, y eso lo hace el servidor en la vista de explore.
// El unico atajo es el boton "Mark all read" del panel, cuyo POST viaja de
// fondo y apaga insignia y negritas en el sitio.
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

  function open() {
    return !panel.hidden;
  }

  function show(visible) {
    panel.hidden = !visible;
    bell.setAttribute('aria-expanded', visible ? 'true' : 'false');
  }

  bell.addEventListener('click', function (event) {
    event.preventDefault();
    show(!open());
  });

  // La insignia y el nombre accesible del boton dicen lo mismo, asi que se
  // apagan juntos; las negritas del panel tambien, porque el servidor acaba
  // de marcarlo todo y el panel debe contar la misma verdad.
  function clearUnseen() {
    const badge = bell.querySelector('.bell-badge');
    if (badge) badge.remove();
    const label = bell.querySelector('.visually-hidden');
    if (label && label.dataset.quiet) label.textContent = label.dataset.quiet;
    panel.querySelectorAll('.bell-item.unseen').forEach(function (item) {
      item.classList.remove('unseen');
    });
    const markall = panel.querySelector('.bell-markall');
    if (markall) markall.remove();
  }

  // El marcado es del servidor; esto solo se lo pide sin recargar.  Falla en
  // silencio a proposito: si la peticion no sale, el POST clasico del
  // formulario sigue disponible y la siguiente carga ensena la verdad.
  const markall = panel.querySelector('.bell-markall');
  if (markall && window.fetch) {
    markall.addEventListener('submit', function (event) {
      event.preventDefault();
      const body = new FormData(markall);
      body.delete('back');
      fetch(markall.action, {
        method: 'POST',
        body: body,
        credentials: 'same-origin',
        headers: { 'X-Requested-With': 'fetch' },
      }).then(function (response) {
        if (response.ok) clearUnseen();
      }).catch(function () {});
    });
  }

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
