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
const PARAMS_TABS = [
  { id: 'parameters', label: 'Parameters' },
  { id: 'stats', label: 'Scenario Stats' },
];

/**
 * How a contest's districting reads.
 *
 * "N × M" only describes a contest that actually has districts. One citywide
 * district is an at-large election, and writing it as 1 × 15 describes a
 * districting nobody drew; the synthetic entry standing for a hybrid run's two
 * contests together is tagged with no districts at all, and printing 0 × 15
 * describes nothing. Both are named for what they are instead.
 */
function districtingLabel(numDistricts, perDistrict, seats) {
  const total = `${seats} seat${seats === 1 ? '' : 's'}`;
  if (numDistricts === 0) return `All contests combined (${total})`;
  if (numDistricts === 1) return `At-large (${total})`;
  return `${numDistricts} × ${perDistrict} (${total})`;
}

/** A two- or three-column table of label against value(s), as in Scenario Stats. */
function keyValueTable(mount, title, headers, rows) {
  if (!rows.length) return;
  if (title) mount.append('div').attr('class', 'matrix-title mt').text(title);
  const wrap = mount.append('div').attr('class', 'params-matrix');
  const table = wrap.append('table').attr('class', 'stats-table');

  const head = table.append('thead').append('tr');
  headers.forEach((label, i) => head.append('th')
    .attr('scope', 'col')
    .attr('class', i ? 'num' : null)
    .text(label));

  table.append('tbody').selectAll('tr').data(rows).join('tr')
    .each(function (row) {
      const tr = d3.select(this);
      tr.append('th').attr('scope', 'row').text(row[0]);
      row.slice(1).forEach((value) => tr.append('td').attr('class', 'num').text(value));
    });
}

function renderRunParams(section, entry, runMeta, manifest, source, selected, tabs) {
  const mount = d3.select(section).select(`[data-params="${entry.slug}"]`);
  if (mount.empty()) return;
  mount.selectAll('*').remove();

  const activeTab = (tabs && tabs.current) || PARAMS_TABS[0].id;

  /*
   * Everything below describes the selected contest, not the run.
   *
   * A composed section's parameters belong to the selected system: its systems
   * come from different runs, simulated under different turnout, pools and
   * matrices, so reading them off the section would describe one run's setup
   * under every option in the dropdown.
   *
   * A run with more than one contest -- a hybrid's two tiers, Basic's three
   * rules -- has a districting, a pool and a plan count per contest, and
   * reporting the run's whole list under whichever one is on show described the
   * others as much as it described that one. Composed sections resolved this at
   * build time; ordinary runs carry it on the system the dropdown selected.
   */
  const numDistricts = source ? source.numDistricts : (selected && selected.numDistricts);
  const perDistrict = source ? source.winners : (selected && selected.perDistrict);
  const totalSeats = source ? source.seats : ((selected && selected.winners) ?? runMeta.totalSeats);
  const shape = `${numDistricts} × ${perDistrict}`;

  // Config rows are per (run, contest), so match the shape as well as the run:
  // matching on the run alone hands a multi-contest run its first contest's
  // pool whichever one is selected.
  const byRun = (manifest.configReference || []).filter((r) => r.run === runMeta.runName);
  const cfg = source || byRun.find((r) => r.shape === shape) || byRun[0] || {};
  const rates = (t) => Object.entries(t || {})
    .map(([bloc, rate]) => `${bloc} ${rate}`).join(' · ') || '—';

  const plans = source ? source.plans
    : ((selected && selected.plans) ?? d3.sum(runMeta.districtConfigs, (dc) => dc.plans));
  const replicates = source ? source.replicates : runMeta.replicates;
  const turnout = source ? source.turnout : runMeta.turnout;
  const primaryTurnout = source ? source.primaryTurnout : runMeta.primaryTurnout;

  // A matrix whose cells are all equal says one number, and says it in one line
  // instead of twenty-five cells.
  const flat = (m) => Object.values(m || {}).flatMap((row) => Object.values(row));
  const uniform = (m) => flat(m).length > 0 && new Set(flat(m)).size === 1;

  const rows = [
    ['Districting', districtingLabel(numDistricts, perDistrict, totalSeats)],
  ];
  // Which run this system was simulated in, shown only where the section mixes
  // several: on an ordinary run it would repeat the heading above it.
  if (source) rows.unshift(['Simulation', source.runName]);
  // No focal-group row: it is chosen in the control bar above the figures, so a
  // fixed one here would contradict the selector the moment it is changed.
  if (uniform(cfg.alphas)) rows.push(['Dirichlet α', `${flat(cfg.alphas)[0]} (uniform)`]);

  /*
   * Two cards in one, switched between rather than stacked.
   *
   * The parameters are what was configured; the stats are what the scenario is
   * made of -- the electorate it models and the ballots it drew. They are read
   * at different moments and the margin is too narrow to hold both, so they
   * share the space and the tab remembers which was last open (per section, and
   * across the redraws that follow every change of system or model).
   */
  const tabStrip = mount.append('div').attr('class', 'params-tabs');
  tabStrip.selectAll('button').data(PARAMS_TABS).join('button')
    .attr('type', 'button')
    .attr('class', (d) => `params-tab${d.id === activeTab ? ' on' : ''}`)
    .attr('aria-pressed', (d) => d.id === activeTab)
    .text((d) => d.label)
    .on('click', (event, d) => {
      if (d.id === activeTab) return;
      if (tabs && tabs.onSelect) tabs.onSelect(d.id);
    });

  if (activeTab === 'stats') {
    // Elections actually simulated, per voter model, for the contest on show.
    const elections = source
      ? source.electionsByMode
      : (runMeta.districtConfigs.find(
        (dc) => dc.numDistricts === numDistricts && dc.winners === perDistrict,
      ) || {}).electionsByMode;
    renderScenarioStats(mount, entry, runMeta, cfg, source,
      { plans, replicates, elections, modeLabels: manifest.modeLabels });
    return;
  }

  mount.append('dl').attr('class', 'params-list')
    .selectAll('div')
    .data(rows)
    .join('div')
    .each(function ([term, value]) {
      const row = d3.select(this);
      row.append('dt').text(term);
      row.append('dd').text(value);
    });

  const order = ((source && source.slates) || entry.slates || []).map((sl) => sl.id);
  /** Blocs in the run's own order, with anything unlisted kept on the end. */
  const blocOrder = (keys) => [
    ...order.filter((k) => keys.includes(k)),
    ...keys.filter((k) => !order.includes(k)),
  ];

  /*
   * Turnout as a table rather than a run-on line.
   *
   * Four blocs joined by separators is a sentence to be parsed before the one
   * rate anyone came for can be found, and a run modelling a second, lower
   * turnout for a narrowing round doubled its length. As a column per round the
   * two are read against each other, which is the comparison a two-round rule
   * exists to raise.
   */
  const turnoutBlocs = blocOrder(Object.keys(turnout || {}));
  keyValueTable(
    mount,
    'Turnout',
    primaryTurnout ? ['Bloc', 'General', 'Primary'] : ['Bloc', 'Rate'],
    turnoutBlocs.map((bloc) => (primaryTurnout
      // A partial override: a bloc the primary does not name votes at its
      // general-round rate, so the cell repeats rather than emptying.
      ? [bloc, turnout[bloc], primaryTurnout[bloc] ?? turnout[bloc]]
      : [bloc, turnout[bloc]])),
  );

  /*
   * The configured ends of the candidate-pool draw. Its floor is not configured
   * -- it falls out of the rules the run has to satisfy -- so that one is
   * reported under Scenario Stats with the rest of what the run turned out to
   * be made of.
   */
  keyValueTable(mount, 'Candidate availability', ['Per district', 'Candidates'], [
    ['Average', cfg.candidatePoolMean],
    ['Maximum', cfg.candidatePoolMax],
  ].filter(([, v]) => v != null));

  /*
   * Both matrices are laid out in the run's own bloc order, not the order the
   * keys happen to sit in the config file.
   *
   * A config's own key order isn't a stable thing to render from: cohesion and
   * alphas are edited independently, so one config can easily state the same
   * blocs in two different orders between them. Rendering off insertion order
   * would then print two matrices of the same blocs with their axes
   * disagreeing. Values are keyed, so ordering is presentation only.
   */
  drawMatrix(mount, 'Cohesion', cfg.cohesion, d3.format('.2f'), order);
  if (!uniform(cfg.alphas)) drawMatrix(mount, 'Dirichlet α', cfg.alphas, d3.format('.2g'), order);
}

/**
 * What the scenario is made of: the electorate behind it, and the ballots it drew.
 *
 * The VAP shares are the same numbers the proportional-representation line is
 * drawn from, so the dotted line on the figures and the table beside them can
 * never tell different stories about the same bloc. A composed section reads
 * them off the run its selected system was simulated in.
 */
function renderScenarioStats(mount, entry, runMeta, cfg, source, sampling) {
  const slates = (source && source.slates) || entry.slates || runMeta.slates || [];
  const pct = d3.format('.1%');
  const count = (v) => (v == null ? '—' : d3.format(',')(v));

  mount.append('div').attr('class', 'matrix-title').text('Voting-age population');
  const wrap = mount.append('div').attr('class', 'params-matrix');
  const table = wrap.append('table').attr('class', 'stats-table');
  const head = table.append('thead').append('tr');
  head.append('th').attr('scope', 'col').text('Bloc');
  head.append('th').attr('scope', 'col').attr('class', 'num').text('VAP share');

  table.append('tbody').selectAll('tr')
    .data(slates)
    .join('tr')
    // The focal group is what every figure in the section is about, so it is
    // marked here rather than left to be found among the others.
    .attr('class', (d) => (d.id === runMeta.focalGroup ? 'focal' : null))
    .each(function (slate) {
      const row = d3.select(this);
      row.append('th').attr('scope', 'row').text(slate.label);
      row.append('td').attr('class', 'num').text(pct(slate.vapShare));
    });

  // Total, so a reader can see the blocs partition the electorate rather than
  // having to add four percentages themselves.
  const total = d3.sum(slates, (s) => s.vapShare);
  table.append('tfoot').append('tr')
    .call((tr) => {
      tr.append('th').attr('scope', 'row').text('Total');
      tr.append('td').attr('class', 'num').text(pct(total));
    });

  /*
   * The floor of the candidate-pool draw, which is the one figure of the three
   * nobody chose: it is the smallest ballot every rule in the run can actually
   * be run on (settings_generator.minimum_candidates), so it follows from the
   * rules rather than from the config. The mean and maximum that complete the
   * distribution are configured, and are reported as parameters.
   */
  const pool = [
    ['Minimum on ballot', cfg.candidatePoolMin],
  ].filter(([, v]) => v != null);

  const list = (title, items) => {
    if (!items.length) return;
    mount.append('div').attr('class', 'matrix-title mt').text(title);
    mount.append('dl').attr('class', 'params-list')
      .selectAll('div')
      .data(items)
      .join('div')
      .each(function ([term, value]) {
        const row = d3.select(this);
        row.append('dt').text(term);
        row.append('dd').text(value);
      });
  };

  list('Candidates per district', pool);

  /*
   * How much was simulated: districting plans drawn from the chain, and how
   * many times each was voted on. Both are sizes of the experiment rather than
   * settings that shape an outcome, which is why they sit here with the pool
   * rather than among the parameters.
   */
  list('Sampling', [
    ['Plans sampled', count(sampling && sampling.plans)],
    ['Replicates', count(sampling && sampling.replicates)],
  ]);

  /*
   * District elections simulated, per voter model.
   *
   * Shown per model rather than as one total because the models do not all
   * cover the same districts: the Cambridge generator skips a district whose
   * candidate pool drew from only one slate, so its count sits below the
   * ranked models'. That shortfall changes what its row is averaged over, so
   * it belongs on the card rather than being left to be inferred.
   */
  const elections = (sampling && sampling.elections) || {};
  const labels = (sampling && sampling.modeLabels) || {};
  const order = Object.keys(labels).filter((id) => id in elections);
  const rest = Object.keys(elections).filter((id) => !order.includes(id));
  list('Elections simulated', [...order, ...rest].map(
    (id) => [labels[id] || id, count(elections[id])],
  ));
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
const BLOC_MODEL_LABELS = { two: 'Two Bloc', four: 'Four Bloc' };

function buildControls(mount, context, palette, state, onChange, slates) {
  mount.selectAll('*').remove();
  const { slug, systems, models, blocModels, onSystemChange, onBlocModelChange } = context;

  // Present only where a section's systems actually span more than one bloc
  // model (see resolve_composition's "blocModel" tag); an ordinary run has
  // nothing to toggle. Changing it swaps which systems the dropdown below
  // offers and repoints Focal Group at the new selection's own run, so both
  // controls are rebuilt through onBlocModelChange rather than redrawn in
  // place.
  if ((blocModels || []).length > 1) {
    const group = mount.append('div').attr('class', 'control-group');
    group.append('span').attr('class', 'control-label').text('Bloc model');
    const toggles = group.append('div').attr('class', 'model-toggles');
    toggles.selectAll('button').data(blocModels).join('button')
      .attr('type', 'button')
      .attr('class', (d) => `model-toggle${state.blocModel === d ? ' on' : ''}`)
      .attr('aria-pressed', (d) => state.blocModel === d)
      .text((d) => BLOC_MODEL_LABELS[d] || d)
      .on('click', (event, d) => {
        if (state.blocModel === d) return;
        onBlocModelChange(d);
      });
  }

  // A section with one system has nothing to choose between; its name is
  // already in the panel title.
  if (systems.length > 1) {
    const group = mount.append('div').attr('class', 'control-group');
    group.append('label')
      .attr('class', 'control-label')
      .attr('for', `sys-${slug}`)
      .text('Voting system');
    group.append('select')
      .attr('id', `sys-${slug}`)
      .attr('class', 'form-select form-select-sm')
      .on('change', function () {
        state.system = this.value;
        // A composed section's systems come from different runs, which need not
        // model the same voter models -- so changing system can change what the
        // toggles below should even offer.
        (onSystemChange || onChange)();
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
      .attr('for', `slate-${slug}`)
      .text('Focal group');
    slateGroup.append('select')
      .attr('id', `slate-${slug}`)
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

  toggles.selectAll('button').data(models).join('button')
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

  /*
   * The comparison shows one bloc model at a time, as the sections do.
   *
   * Rows from different bloc models cannot share an axis honestly: their slates
   * are different populations -- POC against WHI in one, four groups in the
   * other -- so a focal group picked for one names nothing in the other, and
   * two rows stacked together would be answering different questions. The
   * toggle picks the model; everything below is derived from it.
   */
  const blocModels = [...new Set(all.map((s) => s.blocModel).filter(Boolean))]
    .sort((a, b) => (a === 'two' ? -1 : 0) - (b === 'two' ? -1 : 0));
  let blocModel = blocModels[0] || null;

  /** The series belonging to the bloc model on show. */
  const seriesFor = (model) => (model ? all.filter((s) => s.blocModel === model) : all);

  /** Slates defined by the runs of one bloc model, that model's focal group first. */
  function slatesFor(model) {
    const runSlugs = new Set(seriesFor(model).map((s) => s.runSlug));
    const runs = manifest.runs.filter((r) => runSlugs.has(r.slug));
    const out = [];
    runs.forEach((run) => (run.slates || []).forEach((sl) => {
      if (!out.some((x) => x.id === sl.id)) out.push(sl);
    }));
    const focal = runs[0]?.focalGroup;
    out.sort((a, b) => (b.id === focal) - (a.id === focal));
    return out;
  }

  let slates = slatesFor(blocModel);
  let slate = slates[0]?.id;

  /*
   * Voter models are read off the records themselves rather than a separate
   * manifest field, so the list can never name a model the data does not carry.
   * Pooled last, as everywhere else on the page, but selected first: the pooled
   * average is what a comparison between systems is normally read on.
   *
   * Narrowed to the systems on show, not to everything on offer: a comparison
   * holding only Cumulative rows has no use for Impulsive, and picking it would
   * empty the figure. The list is therefore recomputed as rows are added and
   * removed, and the current pick moved to something the new set has if it no
   * longer does.
   */
  function modesFor(shown) {
    const present = new Set(shown.flatMap((s) => s.records.map((r) => r.mode)));
    return Object.keys(manifest.modeLabels)
      .filter((id) => present.has(id))
      .sort((a, b) => (a === 'combined') - (b === 'combined'));
  }

  let modes = modesFor(all);
  let mode = modes.includes('combined') ? 'combined' : modes[0];

  function draw() {
    try {
      // Manifest order, not the order they were added: rows stay put as the
      // selection grows, and systems from one run stay together.
      renderCrossRun(container, manifest, {
        series: seriesFor(blocModel).filter((s) => selected.has(s.id)),
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

    // The models the rows on show were simulated under. Falls back to the full
    // list while nothing is selected, so the filter is populated rather than
    // empty on first paint.
    const available = seriesFor(blocModel);
    const shown = available.filter((s) => selected.has(s.id));
    modes = modesFor(shown.length ? shown : available);
    if (!modes.includes(mode)) mode = modes.includes('combined') ? 'combined' : modes[0];

    // Two rows: the systems on show, then the filters that apply to all of them.
    // Letting them share a line put the filters at the end of a chip list whose
    // length changes with the selection, so they never sat in the same place.
    const systemsRow = mount.append('div').attr('class', 'control-row');
    const filtersRow = mount.append('div').attr('class', 'control-row');

    // Switching model empties the selection: the rows on show belong to the
    // model that was active when they were added, and carrying them across
    // would put two bloc models on one axis.
    if (blocModels.length > 1) {
      const blocGroup = systemsRow.append('div').attr('class', 'control-group');
      blocGroup.append('span').attr('class', 'control-label').text('Bloc model');
      const toggles = blocGroup.append('div').attr('class', 'model-toggles');
      toggles.selectAll('button').data(blocModels).join('button')
        .attr('type', 'button')
        .attr('class', (d) => `model-toggle${blocModel === d ? ' on' : ''}`)
        .attr('aria-pressed', (d) => blocModel === d)
        .text((d) => BLOC_MODEL_LABELS[d] || d)
        .on('click', (event, d) => {
          if (blocModel === d) return;
          blocModel = d;
          selected.clear();
          slates = slatesFor(blocModel);
          slate = slates[0]?.id;
          controls();
          draw();
        });
    }

    const group = systemsRow.append('div').attr('class', 'control-group');
    group.append('span').attr('class', 'control-label').text('Electoral systems');
    const bar = group.append('div').attr('class', 'system-chips');

    bar.selectAll('span.system-chip')
      .data(available.filter((s) => selected.has(s.id)), (d) => d.id)
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
    const remaining = available.filter((s) => !selected.has(s.id));
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

/** One run's four artifacts, fetched once and shared between sections. */
function loadBundle(cache, key, data) {
  if (!cache.has(key)) {
    cache.set(key, Promise.all([
      json(data.run),
      json(data.focalSeats),
      json(data.slateSeats),
      // Optional: a run whose settings were cleaned up has no availability
      // artifact, and the tab says so rather than the run failing to load.
      data.availability ? json(data.availability) : Promise.resolve(null),
    ]).then(([runMeta, focalSeats, slateSeats, availability]) => (
      { runMeta, focalSeats, slateSeats, availability }
    )));
  }
  return cache.get(key);
}

/**
 * One system's slice of the run it was simulated in.
 *
 * Narrowed by contest, not just by rule: a run can use the same rule in two
 * contests of different sizes, and pooling those would put two districtings in
 * one distribution. The records keep their own system id, so the charts filter
 * by it exactly as they do for an ordinary run.
 */
function sourceBundle(bundles, source) {
  const base = bundles.get(source.run);
  const ofSource = (records) => (records || []).filter(
    (r) => r.system === source.system
      && (r.numDistricts === undefined || r.numDistricts === source.numDistricts)
      && (r.winners === undefined || r.winners === source.winners),
  );
  return {
    ...base,
    focalSeats: ofSource(base.focalSeats),
    slateSeats: ofSource(base.slateSeats),
    availability: base.availability && {
      ...base.availability,
      boxes: base.availability.boxes.filter(
        (b) => b.numDistricts === source.numDistricts && b.winners === source.winners,
      ),
    },
  };
}

async function mountRun(section, manifest, cache) {
  const slug = section.dataset.run;
  const charts = Array.from(section.querySelectorAll('[data-chart]'));
  const entry = manifest.runs.find((r) => r.slug === slug);

  if (!entry) {
    charts.forEach((node) => emptyState(d3.select(node), `No data for ${slug}.`));
    return null;
  }

  // A composed section selects between contests that live in different runs, so
  // it holds one bundle per run it draws on. The cache is shared with every
  // other section, so a run that appears in two of them is fetched once.
  const composition = entry.composition || null;
  const bundles = new Map();

  let bundle;
  try {
    bundle = await loadBundle(cache, slug, entry.data);
    for (const source of composition || []) {
      if (!bundles.has(source.run)) {
        bundles.set(source.run, await loadBundle(cache, source.run, source.data));
      }
    }
  } catch (error) {
    charts.forEach((node) => emptyState(d3.select(node), `Could not load this run: ${error.message}`));
    console.error(error);
    return null;
  }

  const { runMeta } = bundle;

  /*
   * The dropdown's options.
   *
   * Each carries its own contest's seat count, not the section's combined
   * total: a hybrid_election run's citywide total (e.g. 15) is the wrong
   * denominator for the proportional-representation line on a single contest
   * (1 X 6 STV wants 6, 9 X 1 IRV wants 9), so the reference line is rescaled
   * per system rather than taken from the manifest as-is. `winners` on a
   * districtConfigs entry is per-district (see _run_metadata's
   * "seats_per_district"), so a real contest's seat count is numDistricts *
   * winners; the synthetic combined entry is tagged numDistricts=0 with winners
   * already holding the citywide total (_tag_hybrid_combined).
   *
   * A composed section's options were resolved at build time and each names the
   * run it came from -- otherwise identical to a run's own, so everything below
   * treats the two the same.
   */
  const systems = composition
    ? composition.map((source) => ({
      id: source.id, label: source.label, winners: source.seats, source,
    }))
    : runMeta.districtConfigs.flatMap((dc) => dc.systems.map((s) => ({
      ...s,
      winners: dc.numDistricts > 0 ? dc.numDistricts * dc.winners : dc.winners,
      // The contest behind this option, so the card can describe the one on
      // show rather than every contest the run happens to hold.
      numDistricts: dc.numDistricts,
      perDistrict: dc.winners,
      plans: dc.plans,
    })));

  // Every distinct bloc model this section's systems carry (see
  // resolve_composition's "blocModel" tag). Empty for an ordinary run, which
  // has nothing to toggle -- it models one bloc count already.
  const blocModels = [...new Set(
    systems.map((s) => s.source && s.source.blocModel).filter(Boolean),
  )];

  /** Where a selection's records live, and which system identifies them there. */
  function resolve(selection) {
    const source = selection && selection.source;
    return {
      source,
      base: source ? bundles.get(source.run) : bundle,
      systemId: source ? source.system : (selection && selection.id),
    };
  }

  /*
   * The slates one system's own run defines.
   *
   * A composed section's systems can be simulated under different bloc
   * models -- WHI/POC in one run, WAIO/BLK/HIS/AAPI in another -- so Focal
   * Group has to be read off the selected system's own run rather than a
   * fixed list carried by the section. An ordinary run's systems all share one
   * run, so this returns the same list regardless of which is selected.
   */
  function slatesFor(selection) {
    const { source, base } = resolve(selection);
    return (source && source.slates) || (base && base.runMeta.slates) || entry.slates || [];
  }

  /** The slate a selection's own run marks as focal, or the first it has. */
  function defaultSlateId(selection, slateList) {
    const { base } = resolve(selection);
    const focal = base && base.runMeta.focalGroup;
    return (slateList.find((s) => s.id === focal) || slateList[0] || {}).id;
  }

  /** This section's systems narrowed to the bloc model currently on show. */
  function visibleSystems() {
    if (!blocModels.length) return systems;
    return systems.filter((s) => (s.source && s.source.blocModel) === state.blocModel);
  }

  /*
   * The voter models one system was actually simulated under.
   *
   * A run's model list covers everything it did across all of its contests,
   * which is rarely what any one of them used: Basic - 3 X 3 ranks its STV
   * ballots under the two ranked models and scores its Cumulative and Limited
   * ballots under name_cumulative, so the run-level list offers a toggle that
   * can only draw an empty chart on whichever contest is on show. Read off the
   * records, the list is exactly the models that have something to draw.
   *
   * The pooled row is dropped where only one real model exists: an average over
   * a single series is that series, and offering both invites a comparison
   * between a thing and itself.
   */
  function modelsFor(selection) {
    const { source, base, systemId } = resolve(selection);
    const declared = source ? source.voterModels : runMeta.voterModels;
    const present = new Set((base.focalSeats || [])
      .filter((r) => r.system === systemId
        && (!source || r.numDistricts === undefined || r.numDistricts === source.numDistricts)
        && (!source || r.winners === undefined || r.winners === source.winners))
      .map((r) => r.mode));
    const available = declared.filter((m) => present.has(m.id));
    if (!available.length) return declared;
    const real = available.filter((m) => !m.pooled);
    return real.length > 1 ? available : real;
  }

  /*
   * What a system starts with switched on: the models it was simulated under,
   * pooled row excluded.
   *
   * So a ranked contest opens on Impulsive and Deliberative, and a Cumulative or
   * Limited one opens on Name Cumulative -- each system showing the ballots it
   * was actually run with, rather than carrying over a selection from a system
   * that shares none of its models. The pooled average stays one click away
   * wherever there is more than one model for it to average.
   */
  function defaultModels(models) {
    const real = models.filter((m) => !m.pooled);
    return new Set((real.length ? real : models).map((m) => m.id));
  }

  const state = {
    system: systems[0].id,
    blocModel: systems[0].source && systems[0].source.blocModel,
    models: defaultModels(modelsFor(systems[0])),
    slate: defaultSlateId(systems[0], slatesFor(systems[0])),
    metric: 'winRate',
    allGroups: false,
    // Which face of the parameters card is open. Kept in state so it survives
    // the redraws that every other control triggers.
    paramsTab: PARAMS_TABS[0].id,
  };

  /*
   * Switch bloc model: narrow the system list to it, then carry the current
   * selection across if the new list has a system with the same label (same
   * shape and rule, simulated under the other bloc model), so toggling reads
   * as "same contest, different electorate" rather than resetting to
   * whatever happens to be first. Falls back to the first option when it
   * doesn't -- a rule that only exists under one bloc model, e.g. Plurality.
   */
  function switchBlocModel(model) {
    if (state.blocModel === model) return;
    const currentLabel = (systems.find((s) => s.id === state.system) || {}).label;
    state.blocModel = model;
    const visible = visibleSystems();
    const match = visible.find((s) => s.label === currentLabel) || visible[0];
    state.system = match.id;
    refreshControls();
    draw();
  }

  function draw() {
    // Resolve the selection once, so a chart takes plain lists and never has to
    // reach back into the control state.
    const selected = systems.find((s) => s.id === state.system) || systems[0];
    const source = selected && selected.source;
    // A composed section reads from the run the selected system was simulated
    // in, narrowed to that one contest; an ordinary run reads its own bundle.
    const active = source ? sourceBundle(bundles, source) : bundle;
    const activeMeta = active.runMeta;
    const systemId = source ? source.system : state.system;
    const viewSystems = [{ ...selected, id: systemId }];
    const winners = selected ? selected.winners : runMeta.totalSeats;
    const currentSlates = slatesFor(selected);
    const baseSlate = currentSlates.find((s) => s.id === state.slate) || currentSlates[0];
    const view = {
      systems: viewSystems,
      models: modelsFor(selected).filter((m) => state.models.has(m.id)),
      slate: baseSlate && {
        ...baseSlate,
        proportionalSeats: baseSlate.vapShare * winners,
        proportionalLabel: `${baseSlate.label} share of VAP: `
          + `${d3.format('.1%')(baseSlate.vapShare)} (${d3.format('.1f')(baseSlate.vapShare * winners)} seats)`,
      },
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
     *
     * The floor is the selected system's own, so a section holding a 15-seat
     * contest next to a 9-seat one does not measure the smaller against an axis
     * sized for the larger.
     */
    const shown = active.slateSeats.filter(
      (r) => r.system === systemId && r.slate === view.slate.id,
    );
    /*
     * The axis spans the whole body, not the outcomes observed in it.
     *
     * Stopping at the largest result made every chart a different width in
     * seats and hid what was at stake: a bloc winning 6 of 15 looked like it
     * had nearly swept the axis, and two systems filling different numbers of
     * seats could not be read against each other. Running 0..seats says how
     * many there were to win, and leaves the empty stretch visible as the part
     * the focal group never reached. The observed max still raises it, so a
     * result past the nominal count is never clipped.
     */
    view.seatMax = Math.max(winners || 0, d3.max(shown, (r) => r.seats) || 0);

    // Redrawn with the figures: on a composed section the parameters describe
    // the selected system, and they change under it. Switching the card's own
    // tab re-renders only the card -- the figures beside it have not changed.
    renderRunParams(section, entry, activeMeta, manifest, source, selected, {
      current: state.paramsTab,
      onSelect: (id) => {
        state.paramsTab = id;
        renderRunParams(section, entry, activeMeta, manifest, source, selected, {
          current: id, onSelect: (next) => { state.paramsTab = next; draw(); },
        });
      },
    });

    applyView(state.allGroups);
    // Note the absence of a teardown: the charts update what is already on
    // screen so their marks can animate from the old selection to the new one.
    // Clearing the container here would turn every change into a rebuild.
    charts.forEach((node) => {
      const container = d3.select(node);
      const render = RENDERERS[node.dataset.chart];
      if (!render) return;
      try {
        render(container, active, manifest, view);
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
  if (!viewToggle.empty() && slatesFor(systems[0]).length > 1) {
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
  const metrics = d3.select(section).select(`[data-metric="${slug}"]`);

  /*
   * Rebuild the bar for the current selection.
   *
   * Only a change of system needs this: the models on offer belong to the run
   * the selected system was simulated in, so a composed section can gain or
   * lose a toggle when the dropdown moves. Toggling a model redraws without
   * coming through here, so the buttons are not rebuilt under the pointer.
   */
  function refreshControls() {
    const selected = systems.find((s) => s.id === state.system) || systems[0];
    const models = modelsFor(selected);
    const currentSlates = slatesFor(selected);

    // The models are a property of the system now on show, so each one opens
    // on its own defaults rather than inheriting a set that may mean
    // something different, or nothing at all, under it. The slate is kept
    // when the new selection still has it -- switching from 15 X 1 IRV to
    // 9 X 1 IRV under the same bloc model has no reason to forget a deliberate
    // Focal Group choice -- and reset only when it doesn't, which is exactly
    // what happens on a bloc-model switch to a run that doesn't define it.
    // Toggling a model or a focal group redraws without coming through here,
    // so either choice survives everything that leaves it valid.
    state.models = defaultModels(models);
    if (!currentSlates.some((s) => s.id === state.slate)) {
      state.slate = defaultSlateId(selected, currentSlates);
    }

    if (!mount.empty()) {
      buildControls(
        mount,
        {
          slug,
          systems: visibleSystems(),
          models,
          blocModels,
          onSystemChange: () => { refreshControls(); draw(); },
          onBlocModelChange: switchBlocModel,
        },
        manifest.palette, state, draw, currentSlates,
      );
    }

    // The boxplot's own control: it changes what fills the boxes, and it changes
    // which boxes, so it belongs with that figure rather than the shared bar.
    // Present only while the selected system's own shape has availability data
    // behind it -- not just whether the run has any at all. A hybrid run's
    // Combined view has none: candidate pools are a property of a real
    // district, and the pooled citywide total (numDistricts 0) isn't one, so
    // checking the whole run would offer a toggle whose chart can only ever
    // come up empty for that one selection.
    const source = selected && selected.source;
    const active = source ? sourceBundle(bundles, source) : bundle;
    const shapeBoxes = source
      ? (active.availability?.boxes || [])
      : (active.availability?.boxes || []).filter(
        (b) => b.numDistricts === selected.numDistricts && b.winners === selected.winners,
      );
    const hasAvailability = shapeBoxes.length > 0;
    metrics.selectAll('button')
      .data(hasAvailability
        ? [{ id: 'winRate', label: 'Win rate' },
           { id: 'availability', label: 'Candidate availability' }]
        : [])
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

  if (!metrics.empty() || !mount.empty()) refreshControls();

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
