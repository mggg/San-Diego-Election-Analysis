/*
 * Page bootstrap.
 *
 * Fetches manifest.json, then fills every [data-chart] element from the run
 * artifacts it points at. The HTML carries no numbers, so a rebuilt data
 * directory updates the page without the markup changing.
 *
 * Note: this fetches JSON, so opening index.html over file:// fails CORS.
 * Serve the directory (`python -m http.server -d docs`) or use the Pages URL.
 */

import { renderHistogram } from './charts/histogram.js';
import { renderBubble } from './charts/bubble.js';
import { renderSlate } from './charts/slate.js';
import { renderCrossRun } from './charts/crossrun.js';
import { emptyState } from './charts/axes.js';

// Per-run charts only; the cross-run figure has its own mount, since its
// selection is a set of systems drawn from every run rather than one run's.
const RENDERERS = {
  histogram: renderHistogram,
  bubble: renderBubble,
  slate: renderSlate,
};

const json = (path) => fetch(path).then((r) => {
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
});

/** One line under each run heading: what was simulated, in the run's own terms. */
function renderRunMeta(runMeta) {
  const node = d3.select(`[data-run-meta="${runMeta.slug}"]`);
  if (node.empty()) return;
  const shapes = runMeta.districtConfigs
    .map((dc) => `${dc.numDistricts} × ${dc.winners} (${dc.plans} plans, ${dc.systems.length} system${dc.systems.length === 1 ? '' : 's'})`)
    .join('; ');
  const models = runMeta.voterModels.filter((m) => !m.pooled).map((m) => m.label).join(' and ');
  node.text(`${shapes} · ${models} voter models · ${runMeta.replicates} replicates · ${runMeta.proportionalLabel}`);
}

function renderConfigTable(rows) {
  const mount = d3.select('[data-table="config-reference"]');
  if (mount.empty() || !rows || !rows.length) return;

  const wrapper = mount.append('div').attr('class', 'table-responsive mb-3');
  const table = wrapper.append('table').attr('class', 'table table-bordered table-sm align-middle');
  const columns = ['Run', 'Shape', 'Seats', 'Voting rules', 'Voter models', 'Candidate pool', 'Turnout'];
  table.append('thead').append('tr').selectAll('th').data(columns).join('th').text((d) => d);

  const fmtTurnout = (t) => Object.entries(t).map(([k, v]) => `${k} ${v}`).join(', ');
  table.append('tbody').selectAll('tr').data(rows).join('tr')
    .each(function (row) {
      const pool = row.candidatePoolMax == null
        ? '—'
        : `max ${row.candidatePoolMax}, mean ${row.candidatePoolMean}`;
      d3.select(this).selectAll('td')
        .data([
          row.run,
          row.shape,
          row.totalSeats,
          row.rules.join(', '),
          row.voterModels.join(', '),
          pool,
          fmtTurnout(row.turnout) + (row.primaryTurnout ? ` (primary: ${fmtTurnout(row.primaryTurnout)})` : ''),
        ])
        .join('td').text((d) => d);
    });
}

/**
 * The control bar above a run's figures: which voting system to show, and which
 * voter models.
 *
 * One selection drives every chart in the run, so the bubble plot and the
 * histogram are always showing the same thing -- the two figures are two views
 * of one selection, not two independent charts that happen to sit together.
 * Exactly one system is shown at a time; comparing systems is what the cross-run
 * figure is for.
 */
function buildControls(mount, runMeta, palette, state, onChange) {
  mount.selectAll('*').remove();
  const systems = runMeta.districtConfigs.flatMap((dc) => dc.systems);

  // A run with one system has nothing to choose between; its name is already in
  // the panel title.
  if (systems.length > 1) {
    const group = mount.append('div').attr('class', 'control-group');
    group.append('label')
      .attr('class', 'control-label')
      .attr('for', `sys-${runMeta.slug}`)
      .text('Voting system');
    group.append('select')
      .attr('id', `sys-${runMeta.slug}`)
      .attr('class', 'form-select form-select-sm')
      .on('change', function () {
        state.system = this.value;
        onChange();
      })
      .selectAll('option')
      .data(systems)
      .join('option')
      .attr('value', (d) => d.id)
      .property('selected', (d) => d.id === state.system)
      .text((d) => d.label);
  }

  const group = mount.append('div').attr('class', 'control-group');
  group.append('span').attr('class', 'control-label').text('Voter model');
  const toggles = group.append('div').attr('class', 'model-toggles');

  toggles.selectAll('button').data(runMeta.voterModels).join('button')
    .attr('type', 'button')
    .attr('class', (d) => `model-toggle${state.models.has(d.id) ? ' on' : ''}`)
    .attr('aria-pressed', (d) => state.models.has(d.id))
    .html((d) => `<span class="swatch" style="background:${palette[d.id] || '#898781'}"></span>${d.label}`)
    .on('click', function (event, d) {
      // Never let the last model be switched off -- an empty chart reads as a
      // broken one rather than as a deliberate selection.
      if (state.models.has(d.id) && state.models.size === 1) return;
      if (state.models.has(d.id)) state.models.delete(d.id); else state.models.add(d.id);
      d3.select(this).classed('on', state.models.has(d.id)).attr('aria-pressed', state.models.has(d.id));
      onChange();
    });
}

/**
 * Wire up one run: load its artifacts once, build the controls, and redraw its
 * charts whenever the selection changes.
 */
/**
 * The cross-run picker: which electoral systems appear as rows.
 *
 * The figure is built up rather than narrowed down. Every system laid out at
 * once is a wall of long labels that has to be read before anything can be done
 * with it, so this starts empty and offers one "Add" control; the systems on
 * show are the only ones taking up room, and each carries its own remove.
 */
function mountCrossRun(section, manifest) {
  const node = section.querySelector('[data-chart="crossrun"]');
  const mount = d3.select(section).select('[data-controls="crossrun"]');
  if (!node) return;

  const all = manifest.crossRun || [];
  const container = d3.select(node);
  if (!all.length) {
    emptyState(container, 'No completed runs to compare yet.');
    return;
  }

  const selected = new Set();

  // Every slate any selected run defines, focal group first: that is what the
  // comparison is normally read for, and it is the one every run shares.
  const focal = manifest.runs[0]?.focalGroup;
  const slates = [];
  manifest.runs.forEach((run) => (run.slates || []).forEach((slate) => {
    if (!slates.some((s) => s.id === slate.id)) slates.push(slate);
  }));
  slates.sort((a, b) => (b.id === focal) - (a.id === focal));
  let slate = slates[0]?.id;

  // Voter models are read off the records themselves rather than a separate
  // manifest field, so the list can never name a model the data does not carry.
  // Pooled last, as everywhere else on the page, but selected first: the pooled
  // average is what a comparison between systems is normally read on.
  const modeIds = new Set(all.flatMap((s) => s.records.map((r) => r.mode)));
  const modes = Object.keys(manifest.modeLabels)
    .filter((id) => modeIds.has(id))
    .sort((a, b) => (a === 'combined') - (b === 'combined'));
  let mode = modes.includes('combined') ? 'combined' : modes[0];

  function draw() {
    try {
      // Manifest order, not the order they were added: rows stay put as the
      // selection grows, and systems from one run stay together.
      renderCrossRun(container, manifest, {
        series: all.filter((s) => selected.has(s.id)),
        slate,
        mode,
      });
    } catch (error) {
      emptyState(container, `Could not draw this chart: ${error.message}`);
      console.error(error);
    }
  }

  function add(id) { selected.add(id); controls(); draw(); }
  function remove(id) { selected.delete(id); controls(); draw(); }

  /** Two lines per entry -- run above, system beneath -- as in the row labels. */
  function twoLine(node, series, runClass, systemClass) {
    node.append('span').attr('class', runClass).text(series.run);
    node.append('span').attr('class', systemClass).text(series.systemLabel);
  }

  function controls() {
    if (mount.empty()) return;
    mount.selectAll('*').remove();

    // Two rows: the systems on show, then the filters that apply to all of them.
    // Letting them share a line put the filters at the end of a chip list whose
    // length changes with the selection, so they never sat in the same place.
    const systemsRow = mount.append('div').attr('class', 'control-row');
    const filtersRow = mount.append('div').attr('class', 'control-row');

    const group = systemsRow.append('div').attr('class', 'control-group');
    group.append('span').attr('class', 'control-label').text('Electoral systems');
    const bar = group.append('div').attr('class', 'system-chips');

    bar.selectAll('span.system-chip')
      .data(all.filter((s) => selected.has(s.id)), (d) => d.id)
      .join('span')
      .attr('class', 'system-chip')
      .each(function (d) {
        const chip = d3.select(this);
        twoLine(chip.append('span').attr('class', 'chip-text'), d, 'chip-run', 'chip-system');
        chip.append('button')
          .attr('type', 'button')
          .attr('class', 'chip-remove')
          .attr('aria-label', `Remove ${d.panelLabel}`)
          .text('×')
          .on('click', () => remove(d.id));
      });

    // The Add control is present only while something is left to add.
    const remaining = all.filter((s) => !selected.has(s.id));
    if (remaining.length) {
      const dropdown = bar.append('span').attr('class', 'dropdown');
      dropdown.append('button')
        .attr('type', 'button')
        .attr('class', 'add-system')
        .attr('data-bs-toggle', 'dropdown')
        .attr('aria-expanded', 'false')
        .text(selected.size ? 'Add +' : 'Add a system +');

      dropdown.append('ul').attr('class', 'dropdown-menu system-menu')
        .selectAll('li')
        .data(remaining, (d) => d.id)
        .join('li')
        .append('button')
        .attr('type', 'button')
        .attr('class', 'dropdown-item')
        .each(function (d) {
          twoLine(d3.select(this), d, 'item-run', 'item-system');
        })
        .on('click', (event, d) => add(d.id));
    }

    // One slate and one model at a time: a row is a voting system, and splitting
    // it by either as well would make the y axis two things at once.
    picker(filtersRow, 'crossrun-slate', 'Slate', slates, () => slate,
      (value) => { slate = value; },
      (d) => (d.id === focal ? `${d.label} (focal group)` : d.label));

    picker(filtersRow, 'crossrun-mode', 'Voter model', modes.map((id) => ({ id })), () => mode,
      (value) => { mode = value; },
      (d) => manifest.modeLabels[d.id] || d.id);
  }

  /** One labelled <select> in the control bar, hidden when there is no choice. */
  function picker(row, id, label, options, get, set, text) {
    if (options.length < 2) return;
    const group = row.append('div').attr('class', 'control-group');
    group.append('label').attr('class', 'control-label').attr('for', id).text(label);
    group.append('select')
      .attr('id', id)
      .attr('class', 'form-select form-select-sm')
      .on('change', function () { set(this.value); draw(); })
      .selectAll('option')
      .data(options)
      .join('option')
      .attr('value', (d) => d.id)
      .property('selected', (d) => d.id === get())
      .text(text);
  }

  controls();
  draw();
}

async function mountRun(section, manifest, cache) {
  const slug = section.dataset.run;
  const charts = Array.from(section.querySelectorAll('[data-chart]'));
  const entry = manifest.runs.find((r) => r.slug === slug);

  if (!entry) {
    charts.forEach((node) => emptyState(d3.select(node), `No data for ${slug}.`));
    return null;
  }

  let bundle;
  try {
    if (!cache.has(slug)) {
      cache.set(slug, Promise.all([
        json(entry.data.run),
        json(entry.data.focalSeats),
        json(entry.data.slateSeats),
      ]).then(([runMeta, focalSeats, slateSeats]) => ({ runMeta, focalSeats, slateSeats })));
    }
    bundle = await cache.get(slug);
  } catch (error) {
    charts.forEach((node) => emptyState(d3.select(node), `Could not load this run: ${error.message}`));
    console.error(error);
    return null;
  }

  const { runMeta } = bundle;
  renderRunMeta(runMeta);

  const systems = runMeta.districtConfigs.flatMap((dc) => dc.systems);
  const state = {
    system: systems[0].id,
    models: new Set(runMeta.voterModels.map((m) => m.id)),
  };

  function draw() {
    // Resolve the selection once, so a chart takes plain lists and never has to
    // reach back into the control state.
    const view = {
      systems: systems.filter((s) => s.id === state.system),
      models: runMeta.voterModels.filter((m) => state.models.has(m.id)),
    };
    // Note the absence of a teardown: the charts update what is already on
    // screen so their marks can animate from the old selection to the new one.
    // Clearing the container here would turn every change into a rebuild.
    charts.forEach((node) => {
      const container = d3.select(node);
      const render = RENDERERS[node.dataset.chart];
      if (!render) return;
      try {
        render(container, bundle, manifest, view);
      } catch (error) {
        emptyState(container, `Could not draw this chart: ${error.message}`);
        console.error(error);
      }
    });
  }

  const mount = d3.select(section).select(`[data-controls="${slug}"]`);
  if (!mount.empty()) buildControls(mount, runMeta, manifest.palette, state, draw);
  draw();
  return draw;
}

async function main() {
  let manifest;
  try {
    manifest = await json('data/manifest.json');
  } catch (error) {
    d3.selectAll('.chart').each(function () {
      emptyState(d3.select(this), 'Data could not be loaded. Serve this directory over http rather than opening the file directly.');
    });
    console.error(error);
    return;
  }

  renderConfigTable(manifest.configReference);

  const comparison = document.querySelector('#comparison');
  if (comparison) mountCrossRun(comparison, manifest);

  const cache = new Map();
  const sections = Array.from(document.querySelectorAll('section.run[data-run]'));
  const redraws = new Map();
  await Promise.all(sections.map(async (section) => {
    redraws.set(section.dataset.run, await mountRun(section, manifest, cache));
  }));

  // The inactive tab mounts hidden, where an SVG has no width to measure;
  // redraw its own run once the tab is shown.
  document.querySelectorAll('[data-bs-toggle="tab"]').forEach((tab) => {
    const slug = tab.closest('section.run[data-run]')?.dataset.run;
    tab.addEventListener('shown.bs.tab', () => redraws.get(slug)?.());
  });
}

main();
