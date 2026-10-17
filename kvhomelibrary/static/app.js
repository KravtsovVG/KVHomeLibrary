(function () {
  "use strict";

  const modal = document.getElementById("modal");
  const modalBody = document.getElementById("modal-body");
  const modalError = document.getElementById("modal-error");
  const toast = document.getElementById("toast");
  let toastTimer = null;

  function showToast(message, isError) {
    if (!toast) return;
    toast.textContent = message || "";
    toast.classList.toggle("error", Boolean(isError));
    toast.hidden = false;
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toast.hidden = true;
    }, isError ? 7000 : 4000);
  }

  function setModalError(message) {
    if (!modalError) return;
    modalError.textContent = message || "";
    modalError.hidden = !message;
  }

  function setDownloadError(form, message) {
    let box = form.querySelector(".download-error");
    if (!box) {
      box = document.createElement("div");
      box.className = "download-error form-error";
      box.setAttribute("role", "alert");
      form.appendChild(box);
    }
    box.textContent = message || "";
    box.hidden = !message;
  }

  function openModal() {
    if (!modal) return;
    modal.hidden = false;
    modal.setAttribute("aria-hidden", "false");
    document.body.classList.add("modal-open");
  }

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    modal.setAttribute("aria-hidden", "true");
    document.body.classList.remove("modal-open");
    if (modalBody) modalBody.innerHTML = "";
    setModalError("");
  }

  async function postForm(form, submitter) {
    const data = new URLSearchParams();
    data.append("ajax", "1");
    new FormData(form).forEach(function (value, key) {
      if (typeof value === "string") data.append(key, value);
    });
    if (submitter && submitter.name) {
      data.append(submitter.name, submitter.value);
    }
    const response = await fetch(form.action, {
      method: "POST",
      body: data,
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest"
      }
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok || !payload || !payload.ok) {
      const message =
        (payload && payload.error) ||
        "Сервер вернул ошибку " + response.status + ". Попробуйте ещё раз.";
      throw new Error(message);
    }
    return payload;
  }

  async function openEdit(link) {
    if (!modal || !modalBody) return;
    setModalError("");
    modalBody.innerHTML = '<div class="muted">Загрузка формы…</div>';
    openModal();
    try {
      const url = new URL(link.href, window.location.origin);
      url.searchParams.set("ajax", "1");
      const response = await fetch(url.toString(), {
        headers: {
          "Accept": "text/html",
          "X-Requested-With": "XMLHttpRequest"
        }
      });
      if (!response.ok) {
        throw new Error("Не удалось открыть форму редактирования (" + response.status + ").");
      }
      modalBody.innerHTML = await response.text();
      const form = modalBody.querySelector("form");
      if (!form) throw new Error("Форма редактирования не найдена.");

      let clickedSubmitter = null;
      form.addEventListener("click", function (event) {
        const button = event.target.closest('button[type="submit"]');
        if (button) clickedSubmitter = button;
      });

      form.addEventListener("submit", async function (event) {
        event.preventDefault();
        setModalError("");
        const submitter = event.submitter || clickedSubmitter;
        clickedSubmitter = null;
        const buttons = form.querySelectorAll('button[type="submit"]');
        buttons.forEach(function (button) { button.disabled = true; });
        try {
          const payload = await postForm(form, submitter);
          showToast(payload.message || "Изменения сохранены.");
          if (payload.close) {
            closeModal();
            window.setTimeout(function () { window.location.reload(); }, 250);
          }
        } catch (error) {
          setModalError(error.message);
        } finally {
          buttons.forEach(function (button) { button.disabled = false; });
        }
      });

      const first = modalBody.querySelector("input, textarea, select");
      if (first) first.focus();
    } catch (error) {
      setModalError(error.message);
    }
  }

  document.addEventListener("click", function (event) {
    const editLink = event.target.closest(".js-edit-book");
    if (editLink) {
      event.preventDefault();
      openEdit(editLink);
      return;
    }
    const closeControl = event.target.closest("[data-modal-close]");
    if (closeControl && modal && !modal.hidden) {
      event.preventDefault();
      closeModal();
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && modal && !modal.hidden) {
      closeModal();
    }
  });

  document.addEventListener("submit", async function (event) {
    const form = event.target.closest("form.js-download-form");
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    const originalText = button ? button.textContent : "";
    setDownloadError(form, "");
    if (button) {
      button.disabled = true;
      button.textContent = "Скачиваю…";
    }
    try {
      const payload = await postForm(form, event.submitter);
      showToast(payload.message || "Книга скачана в каталог.");
      if (button) button.textContent = "Скачано";
      if (payload.book_url) {
        let link = form.querySelector(".download-result");
        if (!link) {
          link = document.createElement("a");
          link.className = "btn secondary small download-result";
          link.textContent = "Открыть книгу";
          form.appendChild(link);
        }
        link.href = payload.book_url;
      }
    } catch (error) {
      setDownloadError(form, error.message);
      showToast(error.message, true);
      if (button) {
        button.disabled = false;
        button.textContent = originalText;
      }
    }
  });
})();
