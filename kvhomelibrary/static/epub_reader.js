(function () {
  "use strict";

  const body = document.body;
  const viewerElement = document.getElementById("epub-viewer");
  const viewerHost = document.getElementById("epub-viewport");
  const bookUrl = body.dataset.bookUrl;
  const bookId = body.dataset.bookId || "0";
  const tocPanel = document.getElementById("epub-toc");
  const settingsPanel = document.getElementById("epub-settings");
  const tocList = document.getElementById("epub-toc-list");
  const prevButton = document.getElementById("epub-prev");
  const nextButton = document.getElementById("epub-next");
  const pageLabel = document.getElementById("epub-page-label");
  const progressBar = document.getElementById("epub-progress-bar");
  const fontValue = document.getElementById("epub-font-value");
  const exitFullscreenButton = document.getElementById("epub-exit-fullscreen");
  const toast = document.getElementById("epub-toast");
  const settingsKey = "kvhl.epub.settings";
  const positionKey = "kvhl.epub.position." + bookId;

  if (!viewerElement || !viewerHost || !bookUrl) return;
  if (typeof window.ePub !== "function") {
    showError("Библиотека epub.js не загрузилась. Скачайте EPUB или обновите страницу.");
    return;
  }

  const settings = {
    flow: "paginated",
    fontSize: 100,
    theme: "light"
  };
  const stored = readJson(settingsKey);
  if (stored) {
    if (stored.flow === "paginated" || stored.flow === "scrolled") settings.flow = stored.flow;
    if (["light", "sepia", "dark"].includes(stored.theme)) settings.theme = stored.theme;
    settings.fontSize = Math.min(180, Math.max(70, Number(stored.fontSize) || 100));
  }

  let rendition = null;
  let book = null;
  let currentCfi = "";
  let toastTimer = null;
  let resizeTimer = null;

  function readJson(key) {
    try {
      return JSON.parse(window.localStorage.getItem(key) || "null");
    } catch (error) {
      return null;
    }
  }

  function saveJson(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (error) { /* ignore */ }
  }

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () { toast.hidden = true; }, 4500);
  }

  function showError(message) {
    viewerElement.replaceChildren();
    const box = document.createElement("div");
    box.className = "epub-error";
    const title = document.createElement("strong");
    title.textContent = "Не удалось открыть EPUB";
    const text = document.createElement("p");
    text.textContent = message;
    const link = document.createElement("a");
    link.className = "btn";
    link.href = "/download/" + bookId;
    link.textContent = "Скачать файл";
    box.append(title, text, link);
    viewerElement.appendChild(box);
  }

  function applyToolbar() {
    body.dataset.readerTheme = settings.theme;
    if (fontValue) fontValue.textContent = settings.fontSize + "%";
    document.querySelectorAll("[data-epub-flow]").forEach(function (button) {
      button.classList.toggle("active", button.dataset.epubFlow === settings.flow);
    });
    document.querySelectorAll("[data-epub-theme]").forEach(function (button) {
      button.classList.toggle("active", button.dataset.epubTheme === settings.theme);
    });
    saveJson(settingsKey, settings);
  }

  function applyTheme() {
    if (!rendition) return;
    const colors = {
      light: { background: "#fbfaf7", color: "#1f2933" },
      sepia: { background: "#f4ecd8", color: "#3b3024" },
      dark: { background: "#111827", color: "#e5e7eb" }
    };
    const selected = colors[settings.theme] || colors.light;
    rendition.themes.register("kvhl-light", {
      body: { background: colors.light.background, color: colors.light.color }
    });
    rendition.themes.register("kvhl-sepia", {
      body: { background: colors.sepia.background, color: colors.sepia.color }
    });
    rendition.themes.register("kvhl-dark", {
      body: { background: colors.dark.background, color: colors.dark.color }
    });
    rendition.themes.select("kvhl-" + settings.theme);
    rendition.themes.fontSize(settings.fontSize + "%");
    applyToolbar();
  }

  function updateProgress(location) {
    if (!location || !book || !book.locations) return;
    let percent = 0;
    if (book.locations.length()) {
      percent = book.locations.percentageFromCfi(location.start.cfi);
    }
    if (progressBar) progressBar.style.width = Math.max(0, Math.min(100, percent * 100)) + "%";
    const displayed = location.start.displayed || {};
    if (pageLabel) {
      if (displayed.page && displayed.total) {
        pageLabel.textContent = displayed.page + " / " + displayed.total;
      } else {
        pageLabel.textContent = Math.round(percent * 100) + "%";
      }
    }
  }

  function renderToc(items, level) {
    items.forEach(function (item) {
      const link = document.createElement("a");
      link.href = "#";
      link.textContent = item.label || item.href;
      link.dataset.epubHref = item.href;
      link.className = "reader-toc-level-" + Math.min(level, 4);
      link.addEventListener("click", function (event) {
        event.preventDefault();
        if (!rendition) return;
        rendition.display(item.href).catch(function () {
          showToast("Не удалось открыть раздел.");
        });
        closePanels();
      });
      tocList.appendChild(link);
      if (item.subitems && item.subitems.length) renderToc(item.subitems, level + 1);
    });
  }

  function loadToc() {
    if (!book) return;
    book.loaded.navigation.then(function (navigation) {
      tocList.replaceChildren();
      if (!navigation.toc.length) {
        const empty = document.createElement("p");
        empty.className = "reader-muted";
        empty.textContent = "Оглавление не найдено.";
        tocList.appendChild(empty);
        return;
      }
      renderToc(navigation.toc, 1);
    }).catch(function () {
      showToast("Не удалось загрузить оглавление.");
    });
  }

  function openBook() {
    const loading = document.createElement("div");
    loading.className = "epub-loading";
    loading.textContent = "Загрузка EPUB…";
    viewerElement.appendChild(loading);
    book = window.ePub(bookUrl, { openAs: "epub" });
    if (book.on) {
      book.on("openFailed", function (error) {
        showError(String(error && error.message ? error.message : error));
      });
    }
    rendition = book.renderTo(viewerElement, {
      width: "100%",
      height: "100%",
      flow: settings.flow === "scrolled" ? "scrolled-doc" : "paginated",
      spread: "auto",
      allowScriptedContent: false
    });

    book.opened.then(function () {
      applyTheme();
      loadToc();
      book.locations.generate(1600).catch(function () { /* ignore */ });
      const position = readJson(positionKey) || {};
      const start = position.cfi || undefined;
      return rendition.display(start).catch(function () {
        return rendition.display();
      });
    }).then(function () {
      loading.remove();
    }).catch(function (error) {
      loading.remove();
      showError(String(error && error.message ? error.message : error));
    });

    rendition.on("displayError", function (error) {
      showError(String(error && error.message ? error.message : error));
    });
    rendition.on("relocated", function (location) {
      currentCfi = location.start.cfi;
      const position = readJson(positionKey) || {};
      position.cfi = currentCfi;
      position.updated = Date.now();
      saveJson(positionKey, position);
      updateProgress(location);
    });
    rendition.on("rendered", function () {
      if (loading.isConnected) loading.remove();
    });
    rendition.on("click", function (event) {
      if (event && event.target && event.target.closest && event.target.closest("a")) return;
      if (settings.flow === "paginated") rendition.next();
    });
  }

  function togglePanel(panel) {
    const opening = panel.hidden;
    if (tocPanel) tocPanel.hidden = true;
    if (settingsPanel) settingsPanel.hidden = true;
    if (panel) panel.hidden = !opening;
  }

  function closePanels() {
    if (tocPanel) tocPanel.hidden = true;
    if (settingsPanel) settingsPanel.hidden = true;
  }

  function setFlow(flow) {
    if (!rendition || !book) return;
    settings.flow = flow;
    applyToolbar();
    const location = currentCfi || undefined;
    rendition.flow(flow === "scrolled" ? "scrolled-doc" : "paginated");
    if (location) {
      rendition.display(location).catch(function () { /* ignore */ });
    }
  }

  function setPseudoFullscreen(active) {
    body.classList.toggle("reader-pseudo-fullscreen", active);
    if (exitFullscreenButton) exitFullscreenButton.hidden = !active;
    if (resizeTimer) window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(function () { if (rendition) rendition.resize(); }, 80);
  }

  function toggleFullscreen() {
    if (body.classList.contains("reader-pseudo-fullscreen")) {
      setPseudoFullscreen(false);
      return;
    }
    const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    if (isIOS) {
      setPseudoFullscreen(true);
      return;
    }
    if (document.fullscreenElement || document.webkitFullscreenElement) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) exit.call(document).catch(function () { /* ignore */ });
      return;
    }
    const request = document.documentElement.requestFullscreen ||
      document.documentElement.webkitRequestFullscreen;
    if (!request) {
      setPseudoFullscreen(true);
      return;
    }
    try {
      const result = request.call(document.documentElement);
      if (result && result.catch) result.catch(function () { setPseudoFullscreen(true); });
    } catch (error) {
      setPseudoFullscreen(true);
    }
  }

  document.addEventListener("click", function (event) {
    const flowButton = event.target.closest("[data-epub-flow]");
    if (flowButton) {
      setFlow(flowButton.dataset.epubFlow);
      return;
    }
    if (event.target.closest("#epub-toc-toggle")) {
      togglePanel(tocPanel);
      return;
    }
    if (event.target.closest("#epub-settings-toggle")) {
      togglePanel(settingsPanel);
      return;
    }
    if (event.target.closest("[data-epub-close]")) {
      closePanels();
      return;
    }
    const fontButton = event.target.closest("[data-epub-font]");
    if (fontButton) {
      settings.fontSize = Math.min(180, Math.max(70, settings.fontSize + Number(fontButton.dataset.epubFont)));
      applyTheme();
      return;
    }
    const themeButton = event.target.closest("[data-epub-theme]");
    if (themeButton) {
      settings.theme = themeButton.dataset.epubTheme;
      applyTheme();
      return;
    }
    if (event.target.closest("#epub-prev")) {
      if (rendition) rendition.prev();
      return;
    }
    if (event.target.closest("#epub-next")) {
      if (rendition) rendition.next();
      return;
    }
    if (event.target.closest("#epub-fullscreen")) {
      toggleFullscreen();
      return;
    }
    if (event.target.closest("#epub-exit-fullscreen")) {
      setPseudoFullscreen(false);
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.target.closest("input, textarea, select")) return;
    if (event.key === "Escape") {
      if (body.classList.contains("reader-pseudo-fullscreen")) setPseudoFullscreen(false);
      closePanels();
      return;
    }
    if (!rendition) return;
    if (event.key === "ArrowRight" || event.key === "PageDown" || event.key === " ") {
      event.preventDefault();
      rendition.next();
    } else if (event.key === "ArrowLeft" || event.key === "PageUp") {
      event.preventDefault();
      rendition.prev();
    }
  });

  window.addEventListener("resize", function () {
    if (resizeTimer) window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(function () { if (rendition) rendition.resize(); }, 150);
  });

  applyToolbar();
  openBook();
})();
