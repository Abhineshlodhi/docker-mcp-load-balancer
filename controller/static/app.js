let currentFilter = 'all';
let allEvents = [];
let localSliderValue = 50;
let isInteractingWithSlider = false;

// Initialize
document.addEventListener('DOMContentLoaded', () => {
  pollStatus();
  setInterval(pollStatus, 1000);
});

// Toast notification helper
function showToast(message, type = 'success') {
  const toast = document.getElementById('toast');
  toast.textContent = message;
  toast.className = `toast show ${type}`;
  setTimeout(() => {
    toast.className = 'toast';
  }, 3500);
}

// Fetch cluster status and update DOM
async function pollStatus() {
  try {
    const res = await fetch('/status');
    if (!res.ok) return;
    const data = await res.json();
    renderDashboard(data);
  } catch (err) {
    console.error('Failed to poll status:', err);
  }
}

function renderDashboard(data) {
  const containers = data.containers;
  const cA = containers.A;
  const cB = containers.B;
  const eff = data.effective_split;
  const cfg = data.configured_split;

  // Render Container A
  updateContainerCard('a', cA, eff.A);
  // Render Container B
  updateContainerCard('b', cB, eff.B);

  // Split badge
  const splitBadge = document.getElementById('split-mode-badge');
  if (splitBadge) {
    splitBadge.textContent = `Active Split: A=${eff.A}% / B=${eff.B}%`;
    if (eff.A !== cfg.A || eff.B !== cfg.B) {
      splitBadge.textContent += ` (Failover from ${cfg.A}/${cfg.B})`;
    }
  }

  // Update slider & ratio bar if user not dragging
  if (!isInteractingWithSlider) {
    document.getElementById('split-slider').value = cfg.A;
    updateVisualSlider(cfg.A);
  }

  // Update Rollout Stepper
  renderRollout(data.rollout);

  // Update Traffic Gen Stats
  renderTrafficGen(data.traffic_generator);

  // Update Events Log
  if (data.recent_events) {
    allEvents = data.recent_events;
    renderEventsTable();
  }
}

function updateContainerCard(prefix, c, effectivePct) {
  const statusPill = document.getElementById(`status-pill-${prefix}`);
  const inflight = document.getElementById(`metric-inflight-${prefix}`);
  const served = document.getElementById(`metric-served-${prefix}`);
  const effMetric = document.getElementById(`metric-eff-${prefix}`);
  const btnToggleUnhealthy = document.getElementById(`btn-toggle-unhealthy-${prefix}`);

  if (inflight) inflight.textContent = c.in_flight;
  if (served) served.textContent = c.requests_served;
  if (effMetric) effMetric.textContent = `${effectivePct}%`;

  if (statusPill) {
    statusPill.textContent = c.status;
    statusPill.className = `status-pill ${c.status}`;
  }

  if (btnToggleUnhealthy) {
    if (c.healthy) {
      btnToggleUnhealthy.textContent = 'Simulate Failure';
      btnToggleUnhealthy.className = 'btn btn-danger-soft';
    } else {
      btnToggleUnhealthy.textContent = 'Restore Healthy';
      btnToggleUnhealthy.className = 'btn btn-success';
    }
  }
}

// Split Controls
function onSliderChange(val) {
  isInteractingWithSlider = true;
  updateVisualSlider(parseInt(val, 10));
}

function updateVisualSlider(valA) {
  const valB = 100 - valA;
  document.getElementById('val-split-a').textContent = `${valA}%`;
  document.getElementById('val-split-b').textContent = `${valB}%`;

  const barA = document.getElementById('visual-bar-a');
  const barB = document.getElementById('visual-bar-b');
  barA.style.width = `${valA}%`;
  barA.textContent = valA > 15 ? `A (${valA}%)` : '';
  barB.style.width = `${valB}%`;
  barB.textContent = valB > 15 ? `B (${valB}%)` : '';
}

function setPreset(a, b) {
  document.getElementById('split-slider').value = a;
  updateVisualSlider(a);
  applySplit();
}

async function applySplit() {
  isInteractingWithSlider = false;
  const valA = parseInt(document.getElementById('split-slider').value, 10);
  const valB = 100 - valA;

  try {
    const res = await fetch('/split', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ A: valA, B: valB, source: 'dashboard' })
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`Split updated to A: ${valA}% | B: ${valB}%`);
      pollStatus();
    } else {
      showToast(data.detail || 'Failed to update split', 'error');
    }
  } catch (err) {
    showToast('Network error updating split', 'error');
  }
}

// Container Actions
async function startContainer(name) {
  showToast(`Starting Container ${name}...`);
  try {
    const res = await fetch(`/containers/${name}/start?source=dashboard`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      showToast(`Container ${name} started and healthy!`);
      pollStatus();
    } else {
      showToast(data.detail || `Failed to start ${name}`, 'error');
    }
  } catch (err) {
    showToast(`Error starting ${name}`, 'error');
  }
}

async function stopContainer(name) {
  showToast(`Draining and stopping Container ${name}...`);
  try {
    const res = await fetch(`/containers/${name}/stop?source=dashboard`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      showToast(`Container ${name} drained and stopped.`);
      pollStatus();
    } else {
      showToast(data.detail || `Cannot stop ${name}`, 'error');
    }
  } catch (err) {
    showToast(`Error stopping ${name}`, 'error');
  }
}

async function toggleUnhealthy(name) {
  const statusPill = document.getElementById(`status-pill-${name.toLowerCase()}`);
  const isCurrentlyHealthy = statusPill && statusPill.textContent.toLowerCase() === 'healthy';
  const shouldMakeUnhealthy = isCurrentlyHealthy;

  try {
    const res = await fetch(`/containers/${name}/unhealthy?unhealthy=${shouldMakeUnhealthy}&source=dashboard`, {
      method: 'POST'
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`Container ${name} toggled: ${shouldMakeUnhealthy ? 'Unhealthy' : 'Healthy'}`);
      pollStatus();
    } else {
      showToast(data.detail || 'Failed to toggle health', 'error');
    }
  } catch (err) {
    showToast('Error toggling health', 'error');
  }
}

// Safe Rollout
async function startRollout(sourceContainer, targetContainer) {
  showToast(`Initiating safe rollout: ${sourceContainer} -> ${targetContainer}...`);
  try {
    const res = await fetch('/rollout', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        source_container: sourceContainer,
        target_container: targetContainer,
        timeout_seconds: 15.0,
        drain_timeout_seconds: 10.0,
        source: 'dashboard'
      })
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`Rollout complete! 100% traffic on ${targetContainer}`);
      pollStatus();
    } else {
      showToast(data.detail || 'Rollout failed or aborted', 'error');
      pollStatus();
    }
  } catch (err) {
    showToast('Network error during rollout', 'error');
  }
}

function renderRollout(rollout) {
  const badge = document.getElementById('rollout-status-badge');
  const stepStart = document.getElementById('step-start');
  const stepHealth = document.getElementById('step-health');
  const stepShift = document.getElementById('step-shift');
  const stepDrain = document.getElementById('step-drain');
  const stepStop = document.getElementById('step-stop');

  // Reset steps
  [stepStart, stepHealth, stepShift, stepDrain, stepStop].forEach(el => {
    el.className = 'step-item';
  });

  if (!rollout.in_progress) {
    if (rollout.current_step === 'completed') {
      badge.textContent = 'Completed';
      badge.className = 'badge text-green';
      [stepStart, stepHealth, stepShift, stepDrain, stepStop].forEach(el => el.classList.add('done'));
    } else if (rollout.current_step === 'aborted') {
      badge.textContent = 'Aborted';
      badge.className = 'badge text-red';
    } else {
      badge.textContent = 'Idle';
      badge.className = 'badge';
    }
    return;
  }

  badge.textContent = `In Progress (${rollout.source || 'mcp'})`;
  badge.className = 'badge text-purple pulse';

  const step = rollout.current_step;
  if (step === 'starting_target') {
    stepStart.classList.add('active');
  } else if (step === 'waiting_for_target_health') {
    stepStart.classList.add('done');
    stepHealth.classList.add('active');
  } else if (step === 'moving_traffic') {
    stepStart.classList.add('done');
    stepHealth.classList.add('done');
    stepShift.classList.add('active');
  } else if (step === 'draining_old') {
    stepStart.classList.add('done');
    stepHealth.classList.add('done');
    stepShift.classList.add('done');
    stepDrain.classList.add('active');
  } else if (step === 'stopping_old') {
    stepStart.classList.add('done');
    stepHealth.classList.add('done');
    stepShift.classList.add('done');
    stepDrain.classList.add('done');
    stepStop.classList.add('active');
  }
}

// Traffic Generator
async function toggleTrafficGen() {
  const btn = document.getElementById('btn-toggle-traffic');
  const isRunning = btn.textContent.includes('Stop');

  if (isRunning) {
    await fetch('/traffic/stop', { method: 'POST' });
    showToast('Traffic generator stopped');
  } else {
    const rpm = parseInt(document.getElementById('traffic-rpm').value, 10) || 60;
    await fetch('/traffic/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requests_per_minute: rpm })
    });
    showToast(`Traffic generator running at ${rpm} RPM`);
  }
  pollStatus();
}

async function resetTrafficStats() {
  await fetch('/traffic/reset', { method: 'POST' });
  pollStatus();
}

function renderTrafficGen(tg) {
  const badge = document.getElementById('traffic-gen-badge');
  const btn = document.getElementById('btn-toggle-traffic');

  if (tg.running) {
    badge.textContent = `Generating (${tg.target_rpm} RPM)`;
    badge.className = 'badge text-green pulse';
    btn.textContent = 'Stop Traffic';
    btn.className = 'btn btn-danger-soft';
  } else {
    badge.textContent = 'Stopped';
    badge.className = 'badge';
    btn.textContent = 'Start Traffic';
    btn.className = 'btn btn-success';
  }

  document.getElementById('stat-total').textContent = tg.total_sent;
  document.getElementById('stat-success').textContent = tg.total_success;
  document.getElementById('stat-failed').textContent = tg.total_failed;
  document.getElementById('stat-by-a').textContent = tg.served_by_a;
  document.getElementById('stat-by-b').textContent = tg.served_by_b;
}

// Logs Filtering and Rendering
function filterLogs(source) {
  currentFilter = source;
  document.querySelectorAll('.filter-tab').forEach(tab => {
    tab.className = tab.textContent.toLowerCase().includes(source) || (source === 'all' && tab.textContent === 'All')
      ? 'filter-tab active' : 'filter-tab';
  });
  renderEventsTable();
}

function renderEventsTable() {
  const tbody = document.getElementById('logs-tbody');
  if (!tbody) return;

  const filtered = currentFilter === 'all' 
    ? allEvents 
    : allEvents.filter(e => e.source && e.source.toLowerCase() === currentFilter.toLowerCase());

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-state">No events recorded for filter: ${currentFilter}</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map(e => {
    const srcClass = e.source === 'mcp' ? 'source-badge mcp' : (e.source === 'dashboard' ? 'source-badge dashboard' : 'source-badge system');
    const resClass = `result-badge ${e.result || 'info'}`;
    const detailStr = typeof e.details === 'object' ? JSON.stringify(e.details) : String(e.details || '');

    return `
      <tr>
        <td>${e.time_str || e.timestamp.substring(11, 19)}</td>
        <td><span class="${srcClass}">${e.source}</span></td>
        <td><strong>${e.action}</strong></td>
        <td><span class="${resClass}">${e.result}</span></td>
        <td title="${escapeHtml(detailStr)}">${escapeHtml(truncate(detailStr, 65))}</td>
      </tr>
    `;
  }).join('');
}

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function truncate(str, len) {
  return str.length > len ? str.substring(0, len) + '...' : str;
}
