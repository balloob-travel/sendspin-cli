/**
 * Sendspin Embedded Player
 * Auto-connects to the server that serves this page.
 */

const MAX_VOLUME = 100;
const SYNC_GAUGE_RANGE_MS = 50;
const SYNC_DISPLAY_ALPHA = 0.18;
const SYNC_DISPLAY_RESET_MS = 1000;
const UI_ACTIVATION_MS = 550;
const START_HAPTIC_PATTERN = [18, 28, 24];
const STOP_HAPTIC_PATTERN = [14];
const SYNC_CLASSES = ["sync-good", "sync-warn", "sync-bad", "sync-idle"];

// DOM elements
const elements = {
  body: document.body,
  controlCard: document.getElementById("control-card"),
  listenToggleBtn: document.getElementById("listen-toggle-btn"),
  syncPanel: document.getElementById("sync-panel"),
  syncStatus: document.getElementById("sync-status"),
  syncDial: document.getElementById("sync-dial"),
  syncGaugeNeedle: document.getElementById("sync-gauge-needle"),
  shareCard: document.getElementById("share-card"),
  qrCode: document.getElementById("qr-code"),
  shareBtn: document.getElementById("share-btn"),
  shareServerUrl: document.getElementById("share-server-url"),
  castLink: document.getElementById("cast-link"),
};

// Player instance
let player = null;
let syncUpdateInterval = null;
let isListening = false;
let isStarting = false;
let showPostAnimationLabel = false;
let smoothedSyncMs = null;
let lastSyncSampleAtMs = 0;

// Auto-derive server URL from current page location
const serverUrl = `${location.protocol}//${location.host}`;
elements.shareServerUrl.textContent = serverUrl;
elements.shareServerUrl.href = serverUrl;

function wait(ms) {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function triggerHaptic(pattern) {
  if (typeof navigator.vibrate !== "function") {
    return;
  }

  try {
    navigator.vibrate(pattern);
  } catch (err) {
    console.warn("Failed to trigger vibration:", err);
  }
}

function updateGaugeNeedle(syncMs) {
  const clampedSyncMs = Math.max(
    -SYNC_GAUGE_RANGE_MS,
    Math.min(SYNC_GAUGE_RANGE_MS, syncMs),
  );
  const angle = (clampedSyncMs / SYNC_GAUGE_RANGE_MS) * 120;
  elements.syncGaugeNeedle.style.transform = `translateX(-50%) rotate(${angle}deg)`;
}

function resetDisplayedSync() {
  smoothedSyncMs = null;
  lastSyncSampleAtMs = 0;
}

function getDisplayedSyncMs(syncMs) {
  const nowMs = performance.now();
  if (
    smoothedSyncMs === null ||
    nowMs - lastSyncSampleAtMs > SYNC_DISPLAY_RESET_MS
  ) {
    smoothedSyncMs = syncMs;
  } else {
    smoothedSyncMs += (syncMs - smoothedSyncMs) * SYNC_DISPLAY_ALPHA;
  }
  lastSyncSampleAtMs = nowMs;
  return smoothedSyncMs;
}

function setSyncTone(tone) {
  elements.syncStatus.classList.remove(...SYNC_CLASSES);
  elements.syncDial.classList.remove(...SYNC_CLASSES);
  elements.syncGaugeNeedle.classList.remove(...SYNC_CLASSES);
  elements.syncStatus.classList.add(tone);
  elements.syncDial.classList.add(tone);
  elements.syncGaugeNeedle.classList.add(tone);
}

function setSyncDisplay({
  label,
  tone = "sync-idle",
  needleMs = 0,
}) {
  elements.syncStatus.textContent = label;
  updateGaugeNeedle(needleMs);
  setSyncTone(tone);
}

function resetSyncDisplay() {
  resetDisplayedSync();
  setSyncDisplay({
    label: "Waiting",
    tone: "sync-idle",
    needleMs: 0,
  });
}

function updateUiState() {
  const pageIsActive = isListening || isStarting;

  elements.body.classList.toggle("is-listening", pageIsActive);
  elements.body.classList.toggle("is-starting", isStarting);
  elements.controlCard.classList.toggle("is-expanded", pageIsActive);
  elements.syncPanel.setAttribute("aria-hidden", String(!pageIsActive));
  elements.listenToggleBtn.setAttribute("aria-pressed", String(pageIsActive));

  if (isStarting && showPostAnimationLabel) {
    elements.listenToggleBtn.textContent = "Connecting...";
    return;
  }

  elements.listenToggleBtn.textContent = isListening
    ? "Stop Listening"
    : "Start Listening";
}

function handlePlayerStateChange() {
  if (!player) return;
  updateSyncStatus();
}

/**
 * Initialize the Sendspin player (called after user interaction)
 */
async function initPlayer() {
  const { SendspinPlayer } = await sdkImport;

  player = new SendspinPlayer({
    baseUrl: serverUrl,
    onStateChange: handlePlayerStateChange,
  });

  try {
    await player.connect();
    if (syncUpdateInterval) {
      clearInterval(syncUpdateInterval);
    }
    syncUpdateInterval = setInterval(updateSyncStatus, 250);
  } catch (err) {
    if (player) {
      try {
        player.disconnect("user_request");
      } catch (disconnectErr) {
        console.warn("Failed to clean up after connection error:", disconnectErr);
      } finally {
        player = null;
      }
    }
    throw err;
  }
}

/**
 * Update sync status display
 */
function updateSyncStatus() {
  if (!player) return;

  if (!player.isConnected) {
    disconnect();
    return;
  }

  const syncInfo = player.syncInfo ?? {};
  const syncMs =
    typeof syncInfo.syncErrorMs === "number" &&
      Number.isFinite(syncInfo.syncErrorMs)
      ? syncInfo.syncErrorMs
      : null;

  if (!player.isPlaying) {
    resetDisplayedSync();
    setSyncDisplay({
      label: "Waiting",
      tone: "sync-idle",
      needleMs: 0,
    });
    return;
  }

  if (syncMs === null) {
    resetDisplayedSync();
    setSyncDisplay({
      label: "Measuring",
      tone: "sync-idle",
      needleMs: 0,
    });
    return;
  }

  const displayedSyncMs = getDisplayedSyncMs(syncMs);
  const absSyncMs = Math.abs(displayedSyncMs);
  const clockPrecision = syncInfo.clockPrecision;

  if (clockPrecision && clockPrecision !== "precise") {
    setSyncDisplay({
      label: "Syncing",
      tone: "sync-warn",
      needleMs: displayedSyncMs,
    });
    return;
  }

  if (absSyncMs <= 10) {
    setSyncDisplay({
      label: "In Sync",
      tone: "sync-good",
      needleMs: displayedSyncMs,
    });
    return;
  }

  if (absSyncMs <= 25) {
    setSyncDisplay({
      label: "Adjusting",
      tone: "sync-warn",
      needleMs: displayedSyncMs,
    });
    return;
  }

  setSyncDisplay({
    label: "Out of Sync",
    tone: "sync-bad",
    needleMs: displayedSyncMs,
  });
}

async function startListening() {
  if (isListening || isStarting) {
    return;
  }

  isListening = true;
  isStarting = true;
  showPostAnimationLabel = false;
  elements.listenToggleBtn.disabled = true;
  updateUiState();

  setSyncDisplay({
    label: "Connecting",
    tone: "sync-idle",
    needleMs: 0,
  });

  try {
    let connectPromise;

    if (player?.isConnected) {
      connectPromise = Promise.resolve();
    } else {
      if (player) {
        try {
          player.disconnect("user_request");
        } catch (disconnectErr) {
          console.warn("Failed to reset stale player before reconnect:", disconnectErr);
        } finally {
          player = null;
        }
      }
      connectPromise = initPlayer();
    }

    await wait(UI_ACTIVATION_MS);
    showPostAnimationLabel = true;
    updateUiState();

    await connectPromise;

    player.setVolume(MAX_VOLUME);
    player.setMuted(false);
    updateSyncStatus();
  } catch (err) {
    console.error("Connection failed:", err);
    disconnect();
  } finally {
    isStarting = false;
    showPostAnimationLabel = false;
    elements.listenToggleBtn.disabled = false;
    updateUiState();
  }
}

function stopListening() {
  isListening = false;
  isStarting = false;
  showPostAnimationLabel = false;

  if (player?.isConnected) {
    player.setMuted(true);
  }

  resetSyncDisplay();
  updateUiState();
}

/**
 * Disconnect from the server
 */
function disconnect() {
  if (syncUpdateInterval) {
    clearInterval(syncUpdateInterval);
    syncUpdateInterval = null;
  }

  if (player) {
    player.disconnect();
    player = null;
  }

  isListening = false;
  isStarting = false;
  showPostAnimationLabel = false;
  elements.listenToggleBtn.disabled = false;

  resetSyncDisplay();
  updateUiState();
}

// Set up Cast link with server URL
elements.castLink.href = `https://sendspin.github.io/cast/?host=${encodeURIComponent(
  serverUrl,
)}`;

if (["localhost", "127.0.0.1"].includes(location.hostname)) {
  elements.shareCard.textContent = "Sharing disabled when visiting localhost";
}

elements.listenToggleBtn.addEventListener("click", async () => {
  if (isListening) {
    triggerHaptic(STOP_HAPTIC_PATTERN);
    stopListening();
    return;
  }

  triggerHaptic(START_HAPTIC_PATTERN);
  await startListening();
});

const sdkImport = import(
  "https://unpkg.com/@sendspin/sendspin-js@2.0.3/dist/index.js?module",
);

// QR Code generation (using qrcode-generator loaded via script tag)
if (typeof qrcode !== "undefined") {
  const qr = qrcode(0, "M");
  qr.addData(location.href);
  qr.make();
  elements.qrCode.innerHTML = qr.createSvgTag({ cellSize: 4, margin: 2 });
}

// Share button - copy URL to clipboard
elements.shareBtn.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(location.href);
  } catch (err) {
    // Fallback for browsers without clipboard API
    const textArea = document.createElement("textarea");
    textArea.value = location.href;
    document.body.appendChild(textArea);
    textArea.select();
    document.execCommand("copy");
    document.body.removeChild(textArea);
  }
  const origText = elements.shareBtn.textContent;
  elements.shareBtn.textContent = "Copied!";
  setTimeout(() => {
    elements.shareBtn.textContent = origText;
  }, 2000);
});

updateUiState();
resetSyncDisplay();
