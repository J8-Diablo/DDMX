// static/popup.js
// Système de popup pour détacher les panneaux UI dans des fenêtres séparées

(function() {
  // État des popups actives { panelId: { window, placeholder } }
  const activePopups = {};

  function scheduleCloseFromPopup(panelClass) {
    setTimeout(() => {
      try {
        closePopup(panelClass);
      } catch (err) {
        console.warn('[POPUP] close failed:', err);
      }
    }, 0);
  }

  function refreshAppLayout() {
    try {
      if (typeof window.applyLayoutSplit === 'function') {
        window.applyLayoutSplit();
        return;
      }
    } catch (err) {}

    try {
      if (typeof window.updateRigCanvasSize === 'function') {
        window.updateRigCanvasSize();
      }
      if (typeof window.drawRig === 'function') {
        window.drawRig();
      }
    } catch (err) {}
  }

  // Configuration des panneaux
  const PANEL_CONFIG = {
    'rig-panel': { title: 'DMX - Rig', minWidth: 600, minHeight: 400 },
    'cues-panel': { title: 'DMX - Cue Lists', minWidth: 500, minHeight: 400 },
    'controller-panel': { title: 'DMX - Controller', minWidth: 700, minHeight: 350 }
  };

  // Crée le bouton popup pour un panneau
  function createPopupButton(panel) {
    const btn = document.createElement('button');
    btn.className = 'popup-btn';
    btn.innerHTML = '⧉';  // Unicode window icon
    btn.title = 'Détacher dans une nouvelle fenêtre';
    btn.onclick = (e) => {
      e.stopPropagation();
      togglePopup(panel);
    };
    return btn;
  }

  // Ajoute les boutons popup à tous les panneaux configurés
  function initPopupButtons() {
    for (const panelId of Object.keys(PANEL_CONFIG)) {
      const panel = document.querySelector(`.${panelId}`);
      if (!panel) continue;

      // Ajoute le bouton dans le header du panneau
      const header = panel.querySelector('.panel-header');
      if (header) {
        const btn = createPopupButton(panel);
        header.style.position = 'relative';
        btn.style.position = 'absolute';
        btn.style.top = '0';
        btn.style.right = '0';
        header.appendChild(btn);
      }
    }
  }

  // Toggle popup pour un panneau
  function togglePopup(panel) {
    const panelClass = Array.from(panel.classList).find(c => PANEL_CONFIG[c]);
    if (!panelClass) return;

    if (activePopups[panelClass]) {
      // Ferme la popup existante
      closePopup(panelClass);
    } else {
      // Ouvre une nouvelle popup
      openPopup(panel, panelClass);
    }
  }

  // Ouvre un panneau dans une popup
  function openPopup(panel, panelClass) {
    const config = PANEL_CONFIG[panelClass];

    // Crée un placeholder pour maintenir le layout
    const placeholder = document.createElement('div');
    placeholder.className = 'panel-placeholder ' + panelClass + '-placeholder';
    placeholder.dataset.forPanel = panelClass;
    placeholder.innerHTML = `<div class="placeholder-text">📤 ${config.title}<br><small>Ouvert dans une fenêtre séparée</small></div>`;

    // Insère le placeholder avant le panneau
    panel.parentNode.insertBefore(placeholder, panel);

    // Ouvre la nouvelle fenêtre
    const width = Math.max(config.minWidth, 800);
    const height = Math.max(config.minHeight, 600);
    const left = (screen.width - width) / 2;
    const top = (screen.height - height) / 2;

    const popup = window.open('', `dmx_popup_${panelClass}`,
      `width=${width},height=${height},left=${left},top=${top},resizable=yes,scrollbars=yes`);

    if (!popup) {
      placeholder.remove();
      alert('Popup bloquée! Autorisez les popups pour ce site.');
      return;
    }

    // Construit le document de la popup
    popup.document.write(`
      <!DOCTYPE html>
      <html lang="fr">
      <head>
        <meta charset="utf-8">
        <title>${config.title}</title>
        <link rel="stylesheet" href="${location.origin}/static/app.css">
        <style>
          html, body { margin: 0; padding: 10px; height: 100%; overflow: auto; }
          .panel { height: calc(100% - 20px); width: 100%; margin: 0; }
          .panel-placeholder { display: none; }
          #rig-canvas { width: 100% !important; height: auto !important; }
        </style>
      </head>
      <body>
        <script>
          (() => {
            const panelClass = ${JSON.stringify(panelClass)};
            const notifyParent = () => {
              try {
                if (window.opener && window.opener.popupManager && typeof window.opener.popupManager.closePopup === 'function') {
                  window.opener.popupManager.closePopup(panelClass);
                }
              } catch (err) {}
            };
            window.addEventListener('beforeunload', notifyParent);
            window.addEventListener('pagehide', notifyParent);
            window.addEventListener('unload', notifyParent);
          })();
        </script>
      </body>
      </html>
    `);
    popup.document.close();

    // Déplace le panneau dans la popup
    popup.document.body.appendChild(panel);

    // Cache le bouton popup dans la fenêtre popup
    const popupBtn = panel.querySelector('.popup-btn');
    if (popupBtn) {
      popupBtn.innerHTML = '⮐';  // Return icon
      popupBtn.title = 'Retourner à la fenêtre principale';
    }

    // Enregistre la popup
    const closeHandler = () => scheduleCloseFromPopup(panelClass);
    const closePoll = window.setInterval(() => {
      if (!activePopups[panelClass]) {
        window.clearInterval(closePoll);
        return;
      }
      if (!popup || popup.closed) {
        window.clearInterval(closePoll);
        scheduleCloseFromPopup(panelClass);
      }
    }, 250);

    activePopups[panelClass] = {
      window: popup,
      placeholder,
      panel,
      closeHandler,
      closePoll,
      closing: false
    };

    // Gère la fermeture de la popup
    popup.onbeforeunload = closeHandler;
    popup.addEventListener('pagehide', closeHandler);
    popup.addEventListener('unload', closeHandler);

    // Redimensionne le canvas si c'est le rig
    if (panelClass === 'rig-panel') {
      setTimeout(() => resizeRigCanvas(popup), 100);
      popup.onresize = () => resizeRigCanvas(popup);
    }

    updateMainLayout();
    requestAnimationFrame(refreshAppLayout);
  }


  // Ferme une popup et retourne le panneau
  function closePopup(panelClass) {
    const data = activePopups[panelClass];
    if (!data) return;

    if (data.closing) return;
    data.closing = true;

    const { window: popup, placeholder, panel, closeHandler, closePoll } = data;

    // Retourne le panneau à la fenêtre principale
    if (placeholder && placeholder.parentNode) {
      placeholder.parentNode.insertBefore(panel, placeholder);
      placeholder.remove();
    }

    // Restaure le bouton popup
    const popupBtn = panel.querySelector('.popup-btn');
    if (popupBtn) {
      popupBtn.innerHTML = '⧉';
      popupBtn.title = 'Détacher dans une nouvelle fenêtre';
    }

    // Ferme la fenêtre popup si encore ouverte
    if (popup && !popup.closed) {
      popup.onbeforeunload = null;
      try {
        popup.removeEventListener('pagehide', closeHandler);
        popup.removeEventListener('unload', closeHandler);
      } catch (err) {}
      popup.close();
    }

    if (closePoll) {
      window.clearInterval(closePoll);
    }

    delete activePopups[panelClass];

    // Redimensionne le canvas si c'est le rig
    if (panelClass === 'rig-panel' && typeof drawRig === 'function') {
      setTimeout(() => drawRig(), 100);
    }

    updateMainLayout();
    requestAnimationFrame(refreshAppLayout);
  }

  // Redimensionne le canvas du rig dans une popup
  function resizeRigCanvas(popup) {
    const canvas = popup.document.getElementById('rig-canvas');
    if (!canvas) return;

    const panel = canvas.closest('.panel');
    if (!panel) return;

    const rect = panel.getBoundingClientRect();
    const canvasRect = canvas.getBoundingClientRect();
    const availableWidth = Math.max(400, Math.floor(rect.width - 20));
    const availableHeight = Math.max(240, Math.floor(popup.innerHeight - canvasRect.top - 24));

    const ratio = 900 / 520;
    let newWidth = availableWidth;
    let newHeight = newWidth / ratio;

    if (newHeight > availableHeight) {
      newHeight = availableHeight;
      newWidth = newHeight * ratio;
    }

    const targetWidth = Math.max(400, Math.round(newWidth));
    const targetHeight = Math.max(240, Math.round(newHeight));

    canvas.style.width = targetWidth + 'px';
    canvas.style.height = targetHeight + 'px';
    if (canvas.width !== targetWidth) canvas.width = targetWidth;
    if (canvas.height !== targetHeight) canvas.height = targetHeight;

    if (typeof drawRig === 'function') {
      drawRig();
    }
  }

  // Met à jour le layout principal
  function updateMainLayout() {
    const mainGrid = document.querySelector('.main-grid');
    if (!mainGrid) return;

    const visiblePanels = mainGrid.querySelectorAll('.panel:not(.panel-placeholder)');
    const placeholders = mainGrid.querySelectorAll('.panel-placeholder');

    visiblePanels.forEach(p => {
      if (placeholders.length > 0) {
        p.style.flex = '1 1 100%';
      } else {
        p.style.flex = '';
      }
    });
  }

  // Ferme toutes les popups
  function closeAllPopups() {
    for (const panelClass of Object.keys(activePopups)) {
      closePopup(panelClass);
    }
  }

  // Initialisation
  document.addEventListener('DOMContentLoaded', initPopupButtons);
  window.addEventListener('beforeunload', closeAllPopups);

  // Expose pour debug
  window.popupManager = { activePopups, closePopup, closeAllPopups };
})();
