(function () {
  "use strict";

  const body = document.body;
  const viewport = document.getElementById("reader-viewport");
  const flow = document.getElementById("reader-flow");
  if (!viewport || !flow) return;

  const bookId = body.dataset.bookId || "0";
  const modeButtons = Array.from(document.querySelectorAll("[data-reader-mode]"));
  const prevButton = document.getElementById("reader-prev");
  const nextButton = document.getElementById("reader-next");
  const pageLabel = document.getElementById("reader-page-label");
  const progressBar = document.getElementById("reader-progress-bar");
  const tocPanel = document.getElementById("reader-toc");
  const settingsPanel = document.getElementById("reader-settings");
  const toast = document.getElementById("reader-toast");
  const exitFullscreenButton = document.getElementById("reader-exit-fullscreen");
  const fullscreenButton = document.getElementById("reader-fullscreen");
  const fontValue = document.getElementById("reader-font-value");
  const lineValue = document.getElementById("reader-line-value");

  const SETTINGS_KEY = "kvhl.reader.settings";
  const POSITION_KEY = "kvhl.reader.position." + bookId;
  const SPREAD_MIN_WIDTH = 1000;
  const coarsePointer = window.matchMedia && window.matchMedia("(pointer: coarse)").matches;

  let settings = {
    fontSize: 18,
    lineHeight: 1.65,
    theme: "light",
    mode: "scroll"
  };
  let mode = "scroll";
  let page = 0;
  let totalPages = 1;
  let pageWidth = 1;
  let verticalPaging = false;
  let verticalPageOffsets = [0];
  let toastTimer = null;

  function loadSettings() {
    try {
      const stored = JSON.parse(window.localStorage.getItem(SETTINGS_KEY) || "{}");
      settings = Object.assign(settings, stored || {});
    } catch (error) { /* localStorage может быть отключён */ }
    if (!["scroll", "single", "spread"].includes(settings.mode)) settings.mode = "scroll";
    if (!["light", "sepia", "dark"].includes(settings.theme)) settings.theme = "light";
    settings.fontSize = Math.min(28, Math.max(14, Number(settings.fontSize) || 18));
    settings.lineHeight = Math.min(2.2, Math.max(1.2, Number(settings.lineHeight) || 1.65));
  }

  function saveSettings() {
    try {
      window.localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    } catch (error) { /* ignore */ }
  }

  function loadPosition() {
    try {
      return JSON.parse(window.localStorage.getItem(POSITION_KEY) || "{}") || {};
    } catch (error) {
      return {};
    }
  }

  function savePosition() {
    try {
      const position = {
        mode: mode,
        percent: currentPercent(),
        page: page,
        updated: Date.now()
      };
      window.localStorage.setItem(POSITION_KEY, JSON.stringify(position));
    } catch (error) { /* ignore */ }
  }

  function currentPercent() {
    if (mode === "scroll") {
      const max = flow.scrollHeight - flow.clientHeight;
      return max > 0 ? Math.min(1, Math.max(0, flow.scrollTop / max)) : 0;
    }
    return totalPages > 1 ? page / (totalPages - 1) : 0;
  }

  function contentHeight() {
    const styles = window.getComputedStyle(viewport);
    const paddingTop = parseFloat(styles.paddingTop) || 0;
    const paddingBottom = parseFloat(styles.paddingBottom) || 0;
    return Math.max(120, viewport.clientHeight - paddingTop - paddingBottom);
  }

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () { toast.hidden = true; }, 4500);
  }

  function applySettings() {
    document.documentElement.style.setProperty("--reader-font-size", settings.fontSize + "px");
    document.documentElement.style.setProperty("--reader-line-height", String(settings.lineHeight));
    body.dataset.readerTheme = settings.theme;
    if (fontValue) fontValue.textContent = String(settings.fontSize);
    if (lineValue) lineValue.textContent = settings.lineHeight.toFixed(2).replace(/0$/, "");
    document.querySelectorAll("[data-reader-theme]").forEach(function (button) {
      button.classList.toggle("active", button.dataset.readerTheme === settings.theme);
    });
  }

  function setMode(nextMode, restorePosition) {
    if (nextMode === "spread" && window.innerWidth < SPREAD_MIN_WIDTH) {
      showToast("Для двух страниц нужен широкий экран — включена одна страница.");
      nextMode = "single";
    }
    mode = nextMode;
    settings.mode = nextMode;
    saveSettings();
    modeButtons.forEach(function (button) {
      button.classList.toggle("active", button.dataset.readerMode === mode);
    });

    flow.classList.remove("reader-scroll", "reader-paged", "reader-vpaged");
    flow.style.columnWidth = "";
    flow.style.columnGap = "";
    flow.style.height = "100%";
    flow.style.maxWidth = "";
    flow.style.margin = "";
    flow.style.overflowX = "";
    flow.style.overflowY = "";
    flow.scrollLeft = 0;
    flow.scrollTop = 0;

    if (mode === "scroll") {
      flow.classList.add("reader-scroll");
      flow.style.height = contentHeight() + "px";
      flow.style.overflowX = "hidden";
      flow.style.overflowY = "auto";
      viewport.style.overflow = "hidden";
      document.body.classList.remove("reader-mode-paged");
      requestAnimationFrame(function () {
        const position = loadPosition();
        const percent = restorePosition ? Number(position.percent || 0) : 0;
        const max = flow.scrollHeight - flow.clientHeight;
        flow.scrollTop = Math.max(0, Math.min(max, max * percent));
        updateScrollState();
      });
    } else {
      flow.classList.add("reader-paged");
      flow.style.maxWidth = mode === "single" ? "72ch" : "none";
      flow.style.margin = mode === "single" ? "0 auto" : "0";
      viewport.style.overflow = "hidden";
      document.body.classList.add("reader-mode-paged");
      layoutPages(restorePosition);
      savePosition();
    }
  }

  function buildVerticalPageOffsets(height) {
    const offsets = [0];
    const flowTop = flow.getBoundingClientRect().top;
    let current = 0;
    const blocks = flow.querySelectorAll("p, h2, h3, h4, h5, h6, blockquote, .poem, .stanza, table");
    blocks.forEach(function (block) {
      const top = block.getBoundingClientRect().top - flowTop + flow.scrollTop;
      if (top > current + 4 && top - current >= height) {
        offsets.push(top);
        current = top;
      }
    });
    return offsets;
  }

  function pageForOffset(offset) {
    let result = 0;
    for (let index = 0; index < verticalPageOffsets.length; index += 1) {
      if (verticalPageOffsets[index] <= offset) result = index;
      else break;
    }
    return result;
  }

  function layoutPages(restorePosition) {
    const height = contentHeight();
    const width = Math.max(120, flow.clientWidth);
    verticalPaging = mode === "single" && coarsePointer;

    if (verticalPaging) {
      flow.classList.add("reader-vpaged");
      flow.style.height = height + "px";
      flow.style.columnWidth = "";
      flow.style.columnGap = "";
      flow.style.overflowX = "hidden";
      flow.style.overflowY = "hidden";
      flow.scrollTop = 0;
      void flow.offsetWidth;
      verticalPageOffsets = buildVerticalPageOffsets(height);
      totalPages = verticalPageOffsets.length;
      const position = loadPosition();
      const percent = restorePosition ? Number(position.percent || 0) : currentPercent();
      page = Math.max(0, Math.min(totalPages - 1, Math.round(percent * (totalPages - 1))));
      goPage(page, false);
      return;
    }

    const gap = mode === "spread" ? 32 : 0;
    const safety = coarsePointer ? 1 : 0;
    const columnWidth = mode === "spread"
      ? Math.max(180, (width - gap) / 2) + safety
      : width + safety;

    flow.scrollLeft = 0;
    flow.style.height = height + "px";
    flow.style.columnWidth = columnWidth + "px";
    flow.style.columnGap = gap + "px";
    flow.style.overflowX = "auto";
    flow.style.overflowY = "hidden";
    void flow.offsetWidth;

    pageWidth = mode === "spread" ? Math.ceil(width + gap + safety * 2) : Math.ceil(width + safety);
    if (mode === "spread") {
      const columns = Math.max(1, Math.ceil((flow.scrollWidth + gap) / (columnWidth + gap)));
      totalPages = Math.max(1, Math.ceil(columns / 2));
    } else {
      totalPages = Math.max(1, Math.ceil(flow.scrollWidth / pageWidth));
    }
    flow.style.overflowX = "hidden";
    flow.style.overflowY = "hidden";
    const position = loadPosition();
    const percent = restorePosition ? Number(position.percent || 0) : currentPercent();
    page = Math.max(0, Math.min(totalPages - 1, Math.round(percent * (totalPages - 1))));
    goPage(page, false);
  }

  function goPage(nextPage, save) {
    page = Math.max(0, Math.min(totalPages - 1, nextPage));
    if (verticalPaging) {
      flow.scrollTop = verticalPageOffsets[page] || 0;
    } else {
      flow.scrollLeft = page * pageWidth;
    }
    updatePageLabel();
    updateProgress();
    if (save !== false) savePosition();
  }

  function pageCount() {
    return mode === "spread" ? totalPages * 2 : totalPages;
  }

  function updatePageLabel() {
    if (!pageLabel) return;
    if (mode === "scroll") {
      pageLabel.textContent = "Режим прокрутки";
      return;
    }
    if (mode === "spread") {
      const first = page * 2 + 1;
      const last = Math.min(page * 2 + 2, pageCount());
      pageLabel.textContent = first + "–" + last + " / " + pageCount();
    } else {
      pageLabel.textContent = (page + 1) + " / " + totalPages;
    }
  }

  function updateProgress() {
    if (!progressBar) return;
    progressBar.style.width = Math.max(0, Math.min(100, currentPercent() * 100)) + "%";
    if (prevButton) prevButton.setAttribute("aria-disabled", String(mode !== "scroll" && page <= 0));
    if (nextButton) nextButton.setAttribute("aria-disabled", String(mode !== "scroll" && page >= totalPages - 1));
  }

  function updateScrollState() {
    updatePageLabel();
    updateProgress();
    savePosition();
  }

  function nextPage() {
    if (mode === "scroll") {
      flow.scrollBy({ top: flow.clientHeight * 0.88, behavior: "smooth" });
    } else {
      goPage(page + 1, true);
    }
  }

  function prevPage() {
    if (mode === "scroll") {
      flow.scrollBy({ top: -flow.clientHeight * 0.88, behavior: "smooth" });
    } else {
      goPage(page - 1, true);
    }
  }

  function togglePanel(panel) {
    if (!panel) return;
    const opening = panel.hidden;
    if (tocPanel) tocPanel.hidden = true;
    if (settingsPanel) settingsPanel.hidden = true;
    panel.hidden = !opening;
  }

  function closePanels() {
    if (tocPanel) tocPanel.hidden = true;
    if (settingsPanel) settingsPanel.hidden = true;
  }

  function relayoutAfterViewportChange() {
    window.setTimeout(function () {
      if (mode === "scroll") {
        flow.style.height = contentHeight() + "px";
        updateScrollState();
      } else {
        layoutPages(true);
      }
    }, 50);
  }

  function setPseudoFullscreen(active) {
    document.body.classList.toggle("reader-pseudo-fullscreen", active);
    if (exitFullscreenButton) exitFullscreenButton.hidden = !active;
    relayoutAfterViewportChange();
  }

  function toggleFullscreen() {
    if (document.body.classList.contains("reader-pseudo-fullscreen")) {
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
    const element = document.documentElement;
    const request = element.requestFullscreen || element.webkitRequestFullscreen;
    if (!request) {
      setPseudoFullscreen(true);
      return;
    }
    try {
      const result = request.call(element);
      if (result && typeof result.catch === "function") {
        result.catch(function () { setPseudoFullscreen(true); });
      }
    } catch (error) {
      setPseudoFullscreen(true);
    }
  }

  function goToAnchor(anchor) {
    const target = document.getElementById(anchor);
    if (!target) return;
    if (mode === "scroll") {
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    } else if (verticalPaging) {
      const targetTop = target.getBoundingClientRect().top - flow.getBoundingClientRect().top + flow.scrollTop;
      goPage(pageForOffset(targetTop), true);
    } else {
      const targetRect = target.getBoundingClientRect();
      const flowRect = flow.getBoundingClientRect();
      const absolute = targetRect.left - flowRect.left + flow.scrollLeft;
      goPage(Math.floor(absolute / pageWidth), true);
    }
    closePanels();
  }

  let resizeTimer = null;
  window.addEventListener("resize", function () {
    if (resizeTimer) window.clearTimeout(resizeTimer);
    resizeTimer = window.setTimeout(function () {
      if (mode === "spread" && window.innerWidth < SPREAD_MIN_WIDTH) {
        setMode("single", true);
      } else if (mode === "scroll") {
        flow.style.height = contentHeight() + "px";
        updateScrollState();
      } else {
        layoutPages(true);
      }
    }, 160);
  });

  function refreshLayout() {
    if (mode === "scroll") {
      flow.style.height = contentHeight() + "px";
      updateScrollState();
    } else {
      layoutPages(true);
    }
  }

  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(refreshLayout).catch(function () { /* ignore */ });
  }
  window.addEventListener("load", refreshLayout);
  if (window.ResizeObserver) {
    let lastWidth = 0;
    let lastHeight = 0;
    new ResizeObserver(function () {
      if (viewport.clientWidth === lastWidth && viewport.clientHeight === lastHeight) return;
      lastWidth = viewport.clientWidth;
      lastHeight = viewport.clientHeight;
      refreshLayout();
    }).observe(viewport);
  }

  if (flow) {
    flow.addEventListener("scroll", function () {
      if (mode === "scroll") updateScrollState();
    }, { passive: true });
  }

  viewport.addEventListener("wheel", function (event) {
    if (mode !== "scroll") return;
    if (flow.contains(event.target)) return;
    if (Math.abs(event.deltaY) <= Math.abs(event.deltaX)) return;
    event.preventDefault();
    flow.scrollTop += event.deltaY;
  }, { passive: false });

  document.addEventListener("click", function (event) {
    const modeButton = event.target.closest("[data-reader-mode]");
    if (modeButton) {
      setMode(modeButton.dataset.readerMode, true);
      return;
    }
    if (event.target.closest("#reader-toc-toggle")) {
      togglePanel(tocPanel);
      return;
    }
    if (event.target.closest("#reader-settings-toggle")) {
      togglePanel(settingsPanel);
      return;
    }
    const closeButton = event.target.closest("[data-reader-close]");
    if (closeButton) {
      closePanels();
      return;
    }
    const anchor = event.target.closest("[data-reader-anchor]");
    if (anchor) {
      event.preventDefault();
      goToAnchor(anchor.dataset.readerAnchor);
      return;
    }
    const fontButton = event.target.closest("[data-reader-font]");
    if (fontButton) {
      settings.fontSize = Math.min(28, Math.max(14, settings.fontSize + Number(fontButton.dataset.readerFont)));
      applySettings();
      saveSettings();
      if (mode !== "scroll") layoutPages(true);
      return;
    }
    const lineButton = event.target.closest("[data-reader-line]");
    if (lineButton) {
      settings.lineHeight = Math.min(2.2, Math.max(1.2, settings.lineHeight + Number(lineButton.dataset.readerLine) * 0.05));
      applySettings();
      saveSettings();
      if (mode !== "scroll") layoutPages(true);
      return;
    }
    const themeButton = event.target.closest("[data-reader-theme]");
    if (themeButton) {
      settings.theme = themeButton.dataset.readerTheme;
      applySettings();
      saveSettings();
      return;
    }
  });

  function bindPageButton(button, action) {
    if (!button) return;
    button.addEventListener("click", function (event) {
      event.preventDefault();
      action();
    });
  }

  bindPageButton(prevButton, prevPage);
  bindPageButton(nextButton, nextPage);
  bindPageButton(document.querySelector("[data-reader-prev]"), prevPage);
  bindPageButton(document.querySelector("[data-reader-next]"), nextPage);
  bindPageButton(fullscreenButton, toggleFullscreen);
  bindPageButton(exitFullscreenButton, function () { setPseudoFullscreen(false); });

  document.addEventListener("keydown", function (event) {
    if (event.target.closest("input, textarea, select")) return;
    if (event.key === "Escape") {
      if (document.body.classList.contains("reader-pseudo-fullscreen")) {
        setPseudoFullscreen(false);
      }
      closePanels();
      return;
    }
    if (event.key === "ArrowRight" || event.key === "PageDown") {
      if (mode !== "scroll") {
        event.preventDefault();
        nextPage();
      }
    } else if (event.key === "ArrowLeft" || event.key === "PageUp") {
      if (mode !== "scroll") {
        event.preventDefault();
        prevPage();
      }
    } else if (event.key === " ") {
      if (mode !== "scroll") {
        event.preventDefault();
        nextPage();
      }
    } else if (event.key === "Home") {
      if (mode !== "scroll") {
        event.preventDefault();
        goPage(0, true);
      }
    } else if (event.key === "End") {
      if (mode !== "scroll") {
        event.preventDefault();
        goPage(totalPages - 1, true);
      }
    }
  });

  let touchStartX = null;
  let touchStartY = null;
  viewport.addEventListener("touchstart", function (event) {
    if (event.touches.length !== 1) return;
    touchStartX = event.touches[0].clientX;
    touchStartY = event.touches[0].clientY;
  }, { passive: true });
  viewport.addEventListener("touchend", function (event) {
    if (touchStartX === null || event.changedTouches.length !== 1) return;
    const dx = event.changedTouches[0].clientX - touchStartX;
    const dy = event.changedTouches[0].clientY - touchStartY;
    touchStartX = null;
    touchStartY = null;
    if (mode !== "scroll" && Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) {
      if (dx < 0) nextPage(); else prevPage();
    }
  }, { passive: true });

  loadSettings();
  const queryMode = new URLSearchParams(window.location.search).get("mode");
  if (["scroll", "single", "spread"].includes(queryMode)) {
    settings.mode = queryMode;
  }
  applySettings();
  setMode(settings.mode, true);
})();
