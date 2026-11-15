// Adds a ☰ menu button to the header on phones. On wider screens the button is hidden by CSS.
(function () {
  var header = document.querySelector('.site-header');
  var nav = header && header.querySelector('nav');
  if (!nav) return;

  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'menu-toggle';
  btn.textContent = '\u2630';
  btn.setAttribute('aria-label', 'Menu');
  btn.setAttribute('aria-expanded', 'false');
  if (!nav.id) nav.id = 'site-nav';
  btn.setAttribute('aria-controls', nav.id);

  nav.classList.add('menu-nav');
  header.classList.add('has-menu');
  nav.parentNode.insertBefore(btn, nav);

  function setOpen(open) {
    nav.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', String(open));
    btn.textContent = open ? '\u2715' : '\u2630';
  }
  btn.addEventListener('click', function () { setOpen(!nav.classList.contains('open')); });
  nav.addEventListener('click', function (e) { if (e.target.closest('a')) setOpen(false); });
  document.addEventListener('click', function (e) { if (!header.contains(e.target)) setOpen(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setOpen(false); });
})();
