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
import { renderAvailability } from './charts/availability.js';
import { emptyState, reducedMotion } from './charts/axes.js';

// Per-run charts only; the cross-run figure has its own mount, since its
// selection is a set of systems drawn from every run rather than one run's.
const RENDERERS = {
  histogram: renderHistogram,
  bubble: renderBubble,
  slate: renderSlate,
  availability: renderAvailability,
};

// How long the Seat outcomes tab takes to cross-fade between its two views.
const VIEW_FADE_MS = 220;

const json = (path) => fetch(path).then((r) => {
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
});

/**
 * The parameters this run was simulated under, in the margin beside its figures.
 *
 * Read from the run's own artifact and from the config reference rather than
 * written into the template, so a table can never describe a run that has since
 * been re-run with different settings. Everything here is fixed for the run --
 * the controls above change what is drawn, never what was simulated.
 */
function renderRunParams(section, entry, runMeta, manifest) {
  const mount = d3.select(section).select(`[data-params="${entry.slug}"]`);
  if (mount.empty()) return;
  mount.selectAll('*').remove();

  const cfg = (manifest.configReference || []).find((r) => r.run === runMeta.runName) || {};
  const list = (xs) => (xs || []).join(', ') || '—';
  const rates = (t) => Object.entries(t || {})
    .map(([bloc, rate]) => `${bloc} ${rate}`).join(' · ') || '—';

  const shape = runMeta.districtConfigs
    .map((dc) => `${dc.numDistricts} × ${dc.winners}`).join('; ');
  const plans = d3.sum(runMeta.districtConfigs, (dc) => dc.plans);

  // A matrix whose cells are all equal says one number, and says it in one line
  // instead of twenty-five cells.
  const flat = (m) => Object.values(m || {}).flatMap((row) => Object.values(row));
  const uniform = (m) => flat(m).length > 0 && new Set(flat(m)).size === 1;

  const rows = [
    ['Districting', `${shape} (${runMeta.totalSeats} seats)`],
    ['Plans sampled', d3.format(',')(plans)],
    ['Replicates', d3.format(',')(runMeta.replicates)],
    ['Candidate pool', cfg.candidatePoolMax == null
      ? '—' : `max ${cfg.candidatePoolMax}, mean ${cfg.candidatePoolMean}`],
    ['Turnout', rates(runMeta.turnout)],
  ];
  // Only shown when the run actually models a lower-turnout primary; an empty
  // row would imply the two rounds differ when they do not.
  if (runMeta.primaryTurnout) rows.push(['Primary turnout', rates(runMeta.primaryTurnout)]);
  // No focal-group row: it is chosen in the control bar above the figures, so a
  // fixed one here would contradict the selector the moment it is changed.
  if (uniform(cfg.alphas)) rows.push(['Dirichlet α', `${flat(cfg.alphas)[0]} (uniform)`]);

  mount.append('h4').attr('class', 'params-title').text('Simulation parameters');
  mount.append('dl').attr('class', 'params-list')
    .selectAll('div')
    .data(rows)
    .join('div')
    .each(function ([term, value]) {
      const row = d3.select(this);
      row.append('dt').text(term);
      row.append('dd').text(value);
    });

  /*
   * Both matrices are laid out in the run's own bloc order, not the order the
   * keys happen to sit in the config file.
   *
   * Those disagree: alternative_electoral.json writes cohesion as
   * WHI, HIS, AAPI, BAIO and the alpha *columns* as WHI, BAIO, HIS, AAPI, so
   * iterating insertion order would print two matrices of the same blocs with
   * their axes in different orders -- and the alpha matrix with its own rows and
   * columns disagreeing. Values are keyed, so ordering is presentation only.
   */
  const order = (entry.slates || []).map((sl) => sl.id);
  drawMatrix(mount, 'Cohesion', cfg.cohesion, d3.format('.2f'), order);
  if (!uniform(cfg.alphas)) drawMatrix(mount, 'Dirichlet α', cfg.alphas, d3.format('.2g'), order);
}

/**
 * One bloc-by-slate matrix: rows are voter blocs, columns the slates they vote
 * for. Read down a column to see who supports that slate, across a row to see
 * how one bloc splits its support.
 */
function drawMatrix(mount, title, matrix, fmt, order) {
  const present = Object.keys(matrix || {});
  if (!present.length) return;
  // `order` first, then anything the matrix has that it does not name, so a
  // bloc missing from the canonical list is still shown rather than dropped.
  const rank = (keys) => [
    ...(order || []).filter((k) => keys.includes(k)),
    ...keys.filter((k) => !(order || []).includes(k)),
  ];
  const blocs = rank(present);
  const slates = rank(Object.keys(matrix[blocs[0]]));

  const wrap = mount.append('div').attr('class', 'params-matrix');
  wrap.append('div').attr('class', 'matrix-title').text(title);
  const table = wrap.append('table');

  const head = table.append('thead').append('tr');
  head.append('th').attr('scope', 'col').text('');
  head.selectAll('th.slate').data(slates).join('th')
    .attr('class', 'slate').attr('scope', 'col').text((d) => d);

  table.append('tbody').selectAll('tr').data(blocs).join('tr')
    .each(function (bloc) {
      const row = d3.select(this);
      row.append('th').attr('scope', 'row').text(bloc);
      row.selectAll('td').data(slates.map((sl) => matrix[bloc][sl])).join('td')
        // The diagonal is within-group cohesion, the number these matrices are
        // usually read for.
        .attr('class', (d, i) => (slates[i] === bloc ? 'own' : null))
        .text((d) => (d == null ? '—' : fmt(d)));
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
function buildControls(mount, runMeta, palette, state, onChange, slates) {
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

  if ((slates || []).length > 1) {
    const slateGroup = mount.append('div').attr('class', 'control-group');
    slateGroup.append('label')
      .attr('class', 'control-label')
      .attr('for', `slate-${runMeta.slug}`)
      .text('Focal group');
    slateGroup.append('select')
      .attr('id', `slate-${runMeta.slug}`)
      .attr('class', 'form-select form-select-sm')
      .on('change', function () { state.slate = this.value; onChange(); })
      .selectAll('option').data(slates)
      .join('option')
      .attr('value', (d) => d.id)
      .property('selected', (d) => d.id === state.slate)
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
    picker(filtersRow, 'crossrun-slate', 'Focal group', slates, () => slate,
      (value) => { slate = value; },
      (d) => d.label);

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
        // Optional: a run whose settings were cleaned up has no availability
        // artifact, and the tab says so rather than the run failing to load.
        entry.data.availability ? json(entry.data.availability) : Promise.resolve(null),
      ]).then(([runMeta, focalSeats, slateSeats, availability]) => (
        { runMeta, focalSeats, slateSeats, availability }
      )));
    }
    bundle = await cache.get(slug);
  } catch (error) {
    charts.forEach((node) => emptyState(d3.select(node), `Could not load this run: ${error.message}`));
    console.error(error);
    return null;
  }

  const { runMeta } = bundle;
  renderRunParams(section, entry, runMeta, manifest);

  const systems = runMeta.districtConfigs.flatMap((dc) => dc.systems);
  // Slate metadata comes from the manifest, which carries each slate's own
  // reference line already worded by the helper the figures share.
  const slates = entry.slates || [];
  const state = {
    system: systems[0].id,
    models: new Set(runMeta.voterModels.map((m) => m.id)),
    slate: (slates.find((s) => s.id === runMeta.focalGroup) || slates[0] || {}).id,
    metric: 'winRate',
    allGroups: false,
  };

  function draw() {
    // Resolve the selection once, so a chart takes plain lists and never has to
    // reach back into the control state.
    const view = {
      systems: systems.filter((s) => s.id === state.system),
      models: runMeta.voterModels.filter((m) => state.models.has(m.id)),
      slate: slates.find((s) => s.id === state.slate) || slates[0],
      metric: state.metric,
    };
    /*
     * One seat axis for both focal figures.
     *
     * They sit side by side and are read against each other, so the axis is
     * derived once from every row for this system and slate. Each chart deriving
     * its own from the rows it happens to draw put them on different axes: the
     * histogram drops the pooled row and the bubble chart drops empty seat
     * counts, so the two disagreed whenever those differed.
     */
    const shown = bundle.slateSeats.filter(
      (r) => r.system === state.system && r.slate === view.slate.id,
    );
    view.seatMax = Math.max(runMeta.seatMax, d3.max(shown, (r) => r.seats) || 0);

    applyView(state.allGroups);
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

  const pair = d3.select(section).select(`[data-one-group="${slug}"]`);
  const panels = d3.select(section).select(`[data-all-groups="${slug}"]`);
  let onScreen = false;   // which of the two the reader is currently looking at

  /*
   * Show one view, cross-fading when it is actually a change.
   *
   * Sequential rather than overlapped: the two views are different heights, and
   * holding both in the layout at once to dissolve between them would shove the
   * rest of the page up and down mid-transition.
   */
  function applyView(allGroups) {
    const incoming = allGroups ? panels : pair;
    const outgoing = allGroups ? pair : panels;

    if (onScreen === allGroups) {          // first paint, or some other control moved
      incoming.attr('hidden', null).style('opacity', 1);
      outgoing.attr('hidden', true);
      return;
    }
    onScreen = allGroups;

    if (reducedMotion()) {
      incoming.attr('hidden', null).style('opacity', 1);
      outgoing.attr('hidden', true);
      return;
    }
    outgoing.transition().duration(VIEW_FADE_MS).style('opacity', 0)
      .on('end', function () {
        d3.select(this).attr('hidden', true);
        incoming.attr('hidden', null).style('opacity', 0)
          .transition().duration(VIEW_FADE_MS).style('opacity', 1);
      });
  }

  const viewToggle = d3.select(section).select(`[data-view="${slug}"]`);
  if (!viewToggle.empty() && slates.length > 1) {
    viewToggle.selectAll('button')
      .data([{ id: false, label: 'Focal group' }, { id: true, label: 'All groups' }])
      .join('button')
      .attr('type', 'button')
      .attr('class', (d) => `model-toggle${state.allGroups === d.id ? ' on' : ''}`)
      .attr('aria-pressed', (d) => state.allGroups === d.id)
      .text((d) => d.label)
      .on('click', (event, d) => {
        if (state.allGroups === d.id) return;
        state.allGroups = d.id;
        viewToggle.selectAll('button')
          .classed('on', (b) => b.id === d.id)
          .attr('aria-pressed', (b) => b.id === d.id);
        draw();
      });
  }

  const mount = d3.select(section).select(`[data-controls="${slug}"]`);
  if (!mount.empty()) buildControls(mount, runMeta, manifest.palette, state, draw, slates);

  // The boxplot's own control: it changes what fills the boxes, and it changes
  // which boxes, so it belongs with that figure rather than the shared bar.
  const metrics = d3.select(section).select(`[data-metric="${slug}"]`);
  if (!metrics.empty() && bundle.availability) {
    metrics.selectAll('button')
      .data([{ id: 'winRate', label: 'Win rate' },
             { id: 'availability', label: 'Candidate availability' }])
      .join('button')
      .attr('type', 'button')
      .attr('class', (d) => `model-toggle${state.metric === d.id ? ' on' : ''}`)
      .attr('aria-pressed', (d) => state.metric === d.id)
      .text((d) => d.label)
      .on('click', function (event, d) {
        state.metric = d.id;
        metrics.selectAll('button')
          .classed('on', (b) => b.id === d.id)
          .attr('aria-pressed', (b) => b.id === d.id);
        draw();
      });
  }

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
