(function () {
  var html = document.documentElement;
  var FONT_CLASSES = ['font-lg', 'font-xl'];

  function apply(pref) {
    html.classList.toggle('contrast-high', pref.contrast === 'high');
    FONT_CLASSES.forEach(function (c) { html.classList.remove(c); });
    if (pref.font && pref.font !== 'normal') html.classList.add('font-' + pref.font);
  }

  function load() {
    try {
      return JSON.parse(localStorage.getItem('prodap-a11y') || '{}');
    } catch (e) {
      return {};
    }
  }

  function save(pref) {
    localStorage.setItem('prodap-a11y', JSON.stringify(pref));
  }

  var pref = load();
  apply(pref);

  document.addEventListener('DOMContentLoaded', function () {
    var contrastBtn = document.getElementById('toggle-contrast');
    var fontBtn = document.getElementById('toggle-font');
    var fontLevels = ['normal', 'lg', 'xl'];

    if (contrastBtn) {
      contrastBtn.addEventListener('click', function () {
        pref.contrast = pref.contrast === 'high' ? 'normal' : 'high';
        apply(pref);
        save(pref);
      });
    }
    if (fontBtn) {
      fontBtn.addEventListener('click', function () {
        var idx = fontLevels.indexOf(pref.font || 'normal');
        pref.font = fontLevels[(idx + 1) % fontLevels.length];
        apply(pref);
        save(pref);
      });
    }

    document.querySelectorAll('[data-toggle-target]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var target = document.querySelector(btn.getAttribute('data-toggle-target'));
        if (target) target.classList.toggle('open');
      });
    });
  });
})();
