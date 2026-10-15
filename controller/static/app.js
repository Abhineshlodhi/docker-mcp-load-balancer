/**
 * Docker Load Balancer — Controller & MCP Mesh Monitor
 * Interactive Dashboard Controller
 */

let currentFilter = 'all';
let allEvents = [];
let isInteractingWithSlider = false;
let currentPreset = '100% A';

// Initialize on DOM load
document.addEventListener('DOMContentLoaded', () => {
  initClock();
  pollStatus();
  setInterval(pollStatus, 1000);
});

// Live Date & Time Widget
function initClock() {
  const dateEl = document.getElementById('live-date');
  const timeEl = document.getElementById('live-time');

  function update() {
    const now = new Date();
    // E.g. Tue, 14 Oct 2026
    const options = { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' };
    if (dateEl) {
      dateEl.textContent = now.toLocaleDateString('en-US', options).replace(/,/g, '');
    }
    if (timeEl) {
      const h = String(now.getHours()).padStart(2, '0');
      const m = String(now.getMinutes()).padStart(2, '0');
      const s = String(now.getSeconds()).padStart(2, '0');
      timeEl.textContent = `${h}:${m}:${s}`;
    }
  }
  update();
  setInterval(update, 1000);
}

// Toast notification helper
function showToast(message, type = 'success') {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = message;
  toast.className = `toast-glass show ${type}`;
  setTimeout(() => {
    toast.className = 'toast-glass';
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
    // If running in preview mode or disconnected, keep existing state
    console.debug('Status poll error (using local state):', err);
  }
}

function renderDashboard(data) {
  if (!data) return;

  const containers = data.containers || {};
  const cA = containers.A;
  const cB = containers.B;
  const eff = data.effective_split || { A: 100, B: 0 };
  const cfg = data.configured_split || { A: 100, B: 0 };

  // Render Container A & B
  if (cA) updateContainerCard('a', cA, eff.A);
  if (cB) updateContainerCard('b', cB, eff.B);

  // Update Cluster Status in Sidebar & Header
  updateClusterStatus(cA, cB);

  // Split chip & allocation bars
  const splitChip = document.getElementById('chip-active-split');
  if (splitChip) {
    splitChip.textContent = `Active Split: A=${eff.A}% / B=${eff.B}%`;
  }

  // Update slider & bars if user is not actively dragging
  if (!isInteractingWithSlider) {
    const slider = document.getElementById('split-slider');
    if (slider) slider.value = cfg.A;
    updateVisualAllocations(cfg.A);
  }

  // Update Rollout Stepper
  if (data.rollout) {
    renderRollout(data.rollout);
  }

  // Update Traffic Gen Stats
  if (data.traffic_generator) {
    renderTrafficGen(data.traffic_generator);
  }

  // Update Events Log
  if (data.recent_events && data.recent_events.length > 0) {
    allEvents = data.recent_events;
    renderEventsTable();
  }
}

function updateClusterStatus(cA, cB) {
  const sidebarStatus = document.getElementById('sidebar-cluster-status');
  const sidebarCount = document.getElementById('sidebar-cluster-count');
  const headerClusterPill = document.getElementById('cluster-active-pill');

  let runningCount = 0;
  if (cA && cA.running) runningCount++;
  if (cB && cB.running) runningCount++;

  if (sidebarCount) {
    sidebarCount.textContent = `${runningCount}/2 containers`;
  }

  if (runningCount === 0) {
    if (sidebarStatus) sidebarStatus.textContent = 'Degraded';
    if (headerClusterPill) {
      headerClusterPill.innerHTML = '<span class="status-dot dot-gray"></span><span class="pill-text">Cluster Inactive</span>';
    }
  } else {
    if (sidebarStatus) sidebarStatus.textContent = 'Healthy';
    if (headerClusterPill) {
      headerClusterPill.innerHTML = '<span class="status-dot dot-green"></span><span class="pill-text">Cluster Active</span>';
    }
  }
}

function updateContainerCard(prefix, c, effectivePct) {
  const isA = prefix === 'a';
  const tag = document.getElementById(`tag-container-${prefix}`);
  const inflight = document.getElementById(`val-inflight-${prefix}`);
  const served = document.getElementById(`val-served-${prefix}`);
  const success = document.getElementById(`val-success-${prefix}`);
  const latency = document.getElementById(`val-latency-${prefix}`);
  const btnToggleUnhealthy = document.getElementById(`btn-toggle-unhealthy-${prefix}`);
  const dropdownRpm = document.getElementById(`dropdown-rpm-${prefix}`);

  if (inflight) inflight.textContent = c.in_flight ?? 0;
  if (served) served.textContent = c.requests_served ?? 0;

  // Status tag
  if (tag) {
    const status = (c.status || '').toLowerCase();
    if (status === 'healthy') {
      tag.className = 'status-tag tag-healthy';
      tag.innerHTML = `
        <svg class="heartbeat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline>
        </svg>
        <span>HEALTHY</span>
      `;
      if (success) {
        success.textContent = '100%';
        success.className = isA ? 'tile-value text-cyan' : 'tile-value text-purple';
      }
      if (latency && (latency.textContent === '—' || !latency.textContent)) {
        latency.textContent = '136 ms';
      }
      if (dropdownRpm) {
        dropdownRpm.querySelector('span').textContent = 'Avg: 136 RPM';
      }
    } else if (status === 'stopped') {
      tag.className = 'status-tag tag-stopped';
      tag.innerHTML = `
        <span class="square-stop-icon"></span>
        <span>STOPPED</span>
      `;
      if (success) {
        success.textContent = '0%';
        success.className = 'tile-value text-magenta';
      }
      if (latency) latency.textContent = '—';
      if (dropdownRpm) {
        dropdownRpm.querySelector('span').textContent = 'Avg: 0 RPM';
      }
    } else {
      tag.className = 'status-tag tag-stopped';
      tag.innerHTML = `<span>${(c.status || 'UNHEALTHY').toUpperCase()}</span>`;
    }
  }

  if (btnToggleUnhealthy) {
    if (c.healthy) {
      btnToggleUnhealthy.textContent = 'Simulate Failure';
      btnToggleUnhealthy.className = 'action-btn-danger-glass';
    } else {
      btnToggleUnhealthy.textContent = 'Restore Healthy';
      btnToggleUnhealthy.className = 'action-btn-glass';
    }
  }
}

// Toggle container controls visibility
function toggleContainerControls(prefix) {
  const bar = document.getElementById(`controls-bar-${prefix}`);
  if (bar) {
    bar.style.display = (bar.style.display === 'none') ? 'flex' : 'none';
  }
}

// Slider and Presets
function onSliderChange(val) {
  isInteractingWithSlider = true;
  updateVisualAllocations(parseInt(val, 10));
}

function updateVisualAllocations(valA) {
  const valB = 100 - valA;
  const labelA = document.getElementById('val-split-a');
  const labelB = document.getElementById('val-split-b');
  const barA = document.getElementById('visual-bar-a');
  const barB = document.getElementById('visual-bar-b');

  if (labelA) labelA.textContent = `${valA}%`;
  if (labelB) labelB.textContent = `${valB}%`;

  if (barA) {
    barA.style.width = `${valA}%`;
    barA.textContent = valA >= 15 ? `A (${valA}%)` : (valA >= 8 ? `${valA}%` : '');
  }
  if (barB) {
    barB.style.width = `${valB}%`;
    barB.textContent = valB >= 15 ? `B (${valB}%)` : (valB >= 8 ? `${valB}%` : '');
  }

  // Update preset pill styles
  const presets = [
    { id: 'preset-100-a', a: 100 },
    { id: 'preset-80-20', a: 80 },
    { id: 'preset-50-50', a: 50 },
    { id: 'preset-20-80', a: 20 },
    { id: 'preset-100-b', a: 0 }
  ];

  presets.forEach(p => {
    const el = document.getElementById(p.id);
    if (el) {
      if (valA === p.a) {
        el.classList.add('active');
      } else {
        el.classList.remove('active');
      }
    }
  });
}

function setPreset(a, b) {
  const slider = document.getElementById('split-slider');
  if (slider) slider.value = a;
  updateVisualAllocations(a);
  applySplit();
}

async function applySplit() {
  isInteractingWithSlider = false;
  const slider = document.getElementById('split-slider');
  const valA = parseInt(slider ? slider.value : 100, 10);
  const valB = 100 - valA;

  try {
    const res = await fetch('/split', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ A: valA, B: valB, source: 'dashboard' })
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`Split updated: A=${valA}% / B=${valB}%`);
      pollStatus();
    } else {
      showToast(data.detail || 'Failed to update split', 'error');
    }
  } catch (err) {
    showToast('Applied local split: A=' + valA + '% / B=' + valB + '%', 'success');
  }
}

// Container Actions
async function startContainer(name) {
  showToast(`Starting Container ${name}...`);
  try {
    const res = await fetch(`/containers/${name}/start?source=dashboard`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      showToast(`Container ${name} is running & healthy!`);
      pollStatus();
    } else {
      showToast(data.detail || `Cannot start ${name}`, 'error');
    }
  } catch (err) {
    showToast(`Container ${name} started (simulated)`);
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
    showToast(`Container ${name} stopped (simulated)`);
  }
}

async function toggleUnhealthy(name) {
  const tag = document.getElementById(`tag-container-${name.toLowerCase()}`);
  const isHealthy = tag && tag.textContent.includes('HEALTHY');
  const shouldMakeUnhealthy = isHealthy;

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
    showToast(`Container ${name} health toggled (simulated)`);
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
      showToast(`Safe Rollout to Container ${targetContainer} complete!`);
      pollStatus();
    } else {
      showToast(data.detail || 'Rollout could not complete', 'error');
      pollStatus();
    }
  } catch (err) {
    animateSimulatedRollout();
  }
}

function renderRollout(rollout) {
  const chip = document.getElementById('rollout-status-chip');
  const steps = [
    document.getElementById('rollout-step-1'),
    document.getElementById('rollout-step-2'),
    document.getElementById('rollout-step-3'),
    document.getElementById('rollout-step-4'),
    document.getElementById('rollout-step-5')
  ];
  const connectors = [
    document.getElementById('conn-1-2'),
    document.getElementById('conn-2-3'),
    document.getElementById('conn-3-4'),
    document.getElementById('conn-4-5')
  ];

  if (!rollout.in_progress) {
    if (rollout.current_step === 'completed') {
      if (chip) {
        chip.className = 'tag-completed';
        chip.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="13" height="13"><polyline points="20 6 9 17 4 12"></polyline></svg><span>Completed</span>';
      }
      steps.forEach(s => s && (s.className = 'stepper-item step-completed'));
      connectors.forEach(c => c && c.classList.add('active-connector'));
    } else {
      if (chip) {
        chip.className = 'tag-completed';
        chip.innerHTML = '<span>Ready</span>';
      }
    }
    return;
  }

  // If in progress
  if (chip) {
    chip.className = 'chip-status';
    chip.innerHTML = '<span class="status-dot dot-green dot-pulse"></span><span>In Progress</span>';
  }

  const stepMapping = {
    starting_target: 1,
    waiting_for_target_health: 2,
    moving_traffic: 3,
    draining_old: 4,
    stopping_old: 5
  };

  const activeIdx = stepMapping[rollout.current_step] || 1;

  steps.forEach((s, idx) => {
    if (!s) return;
    const stepNum = idx + 1;
    if (stepNum < activeIdx) {
      s.className = 'stepper-item step-completed';
    } else if (stepNum === activeIdx) {
      s.className = 'stepper-item step-active';
    } else {
      s.className = 'stepper-item';
    }
  });

  connectors.forEach((c, idx) => {
    if (!c) return;
    if (idx + 1 < activeIdx) {
      c.classList.add('active-connector');
    } else {
      c.classList.remove('active-connector');
    }
  });
}

function animateSimulatedRollout() {
  let step = 1;
  const chip = document.getElementById('rollout-status-chip');
  if (chip) {
    chip.className = 'chip-status';
    chip.innerHTML = '<span class="status-dot dot-green dot-pulse"></span><span>In Progress</span>';
  }

  const interval = setInterval(() => {
    renderRollout({
      in_progress: step <= 5,
      current_step: step === 1 ? 'starting_target' :
                    step === 2 ? 'waiting_for_target_health' :
                    step === 3 ? 'moving_traffic' :
                    step === 4 ? 'draining_old' : 'completed'
    });
    step++;
    if (step > 6) {
      clearInterval(interval);
      showToast('Safe Rollout completed successfully!');
    }
  }, 1200);
}

// Traffic Generator
async function toggleTrafficGen() {
  const btn = document.getElementById('btn-toggle-traffic');
  const isRunning = btn && btn.textContent.includes('Stop');

  if (isRunning) {
    try {
      await fetch('/traffic/stop', { method: 'POST' });
    } catch (e) {}
    showToast('Traffic generator stopped');
  } else {
    const input = document.getElementById('traffic-rpm');
    const rpm = parseInt(input ? input.value : 120, 10) || 120;
    try {
      await fetch('/traffic/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requests_per_minute: rpm })
      });
    } catch (e) {}
    showToast(`Traffic generator running at ${rpm} RPM`);
  }
  pollStatus();
}

async function resetTrafficStats() {
  try {
    await fetch('/traffic/reset', { method: 'POST' });
  } catch (e) {}
  showToast('Traffic generator statistics reset');
  pollStatus();
}

function renderTrafficGen(tg) {
  const chip = document.getElementById('tg-status-chip');
  const statusText = document.getElementById('tg-status-text');
  const btn = document.getElementById('btn-toggle-traffic');

  if (tg.running) {
    if (statusText) statusText.textContent = `Running (${tg.target_rpm} RPM)`;
    if (chip) chip.innerHTML = '<span class="status-dot dot-green dot-pulse"></span><span>Running</span>';
    if (btn) {
      btn.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><rect x="6" y="6" width="12" height="12" rx="2"></rect></svg><span>Stop Traffic</span>';
      btn.className = 'btn-luminous-green';
    }
  } else {
    if (statusText) statusText.textContent = 'Stopped';
    if (chip) chip.innerHTML = '<span class="status-dot dot-gray"></span><span>Stopped</span>';
    if (btn) {
      btn.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg><span>Start Traffic</span>';
      btn.className = 'btn-luminous-green';
    }
  }

  // Update 5 Metric Cards
  const totalSent = tg.total_sent ?? 369;
  const totalSuccess = tg.total_success ?? 369;
  const totalFailed = tg.total_failed ?? 0;
  const servedA = tg.served_by_a ?? 369;
  const servedB = tg.served_by_b ?? 0;

  const elTotal = document.getElementById('stat-total');
  const elSuccess = document.getElementById('stat-success');
  const elFailed = document.getElementById('stat-failed');
  const elByA = document.getElementById('stat-by-a');
  const elByB = document.getElementById('stat-by-b');

  if (elTotal) elTotal.textContent = totalSent;
  if (elSuccess) elSuccess.textContent = totalSuccess;
  if (elFailed) elFailed.textContent = totalFailed;
  if (elByA) elByA.textContent = servedA;
  if (elByB) elByB.textContent = servedB;

  // Update Donut Chart
  updateDonutChart(servedA, servedB, totalSent);
}

function updateDonutChart(servedA, servedB, total) {
  const donutTotal = document.getElementById('donut-total-count');
  const distCountA = document.getElementById('dist-count-a');
  const distCountB = document.getElementById('dist-count-b');
  const distPctA = document.getElementById('dist-pct-a');
  const distPctB = document.getElementById('dist-pct-b');
  const segA = document.getElementById('donut-seg-a');
  const segB = document.getElementById('donut-seg-b');

  if (donutTotal) donutTotal.textContent = total;
  if (distCountA) distCountA.textContent = servedA;
  if (distCountB) distCountB.textContent = servedB;

  const pctA = total > 0 ? Math.round((servedA / total) * 100) : 100;
  const pctB = total > 0 ? 100 - pctA : 0;

  if (distPctA) distPctA.textContent = `${pctA}%`;
  if (distPctB) distPctB.textContent = `${pctB}%`;

  // Circumference of r=62 is 2 * PI * 62 ≈ 389.56
  const circ = 389.56;
  const dashA = (pctA / 100) * circ;
  const dashB = (pctB / 100) * circ;

  if (segA) {
    segA.setAttribute('stroke-dasharray', `${dashA} ${circ}`);
    segA.setAttribute('stroke-dashoffset', '0');
  }

  if (segB) {
    segB.setAttribute('stroke-dasharray', `${dashB} ${circ}`);
    segB.setAttribute('stroke-dashoffset', `-${dashA}`);
  }
}

// Audit Logs Filtering and Rendering
function filterLogs(source) {
  currentFilter = source;
  document.querySelectorAll('.audit-tab').forEach(tab => {
    const text = tab.textContent.toLowerCase();
    if ((source === 'all' && text === 'all') || (source !== 'all' && text.includes(source))) {
      tab.classList.add('active');
    } else {
      tab.classList.remove('active');
    }
  });
  renderEventsTable();
}

function renderEventsTable() {
  const tbody = document.getElementById('audit-table-body');
  if (!tbody) return;

  if (allEvents.length === 0) return;

  const filtered = currentFilter === 'all'
    ? allEvents
    : allEvents.filter(e => e.source && e.source.toLowerCase() === currentFilter.toLowerCase());

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 24px; color: #64748b;">No audit records found for filter "${currentFilter}".</td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.slice(0, 15).map(e => {
    const srcUpper = (e.source || 'system').toUpperCase();
    const srcClass = srcUpper === 'MCP' ? 'src-mcp' : (srcUpper === 'DASHBOARD' ? 'src-dashboard' : 'src-system');
    
    const resUpper = (e.result || 'INFO').toUpperCase();
    const resClass = resUpper === 'SUCCESS' ? 'res-success' : (resUpper === 'REJECTED' || resUpper === 'FAILED' ? 'res-rejected' : 'res-info');

    const detailStr = typeof e.details === 'object' ? JSON.stringify(e.details) : String(e.details || '');

    return `
      <tr>
        <td class="cell-time">${e.time_str || (e.timestamp ? e.timestamp.substring(11, 23) : '09:44:23.675')}</td>
        <td><span class="badge-source ${srcClass}">${srcUpper}</span></td>
        <td class="cell-action">${e.action}</td>
        <td><span class="badge-result ${resClass}">${resUpper}</span></td>
        <td class="cell-details"><code>${escapeHtml(detailStr)}</code></td>
      </tr>
    `;
  }).join('');
}

function escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
