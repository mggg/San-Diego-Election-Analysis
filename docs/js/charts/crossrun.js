/*
 * Cross-run comparison: one bubble row per electoral system, on one shared axis
 * of seat share, banded into deciles.
 *
 * The browser counterpart of plot_combined_bubbles_all_runs, but a chart the
 * static figure cannot be: which systems appear is chosen on the page, and any
 * number of them can sit side by side. A row is a (run, system) pair, and rows
 * from different runs are directly comparable because they share the axis.
 *
 * One slate and one voter model at a time, the focal group and the pooled
 * "combined" average by default. Drawing every model at once would triple the
 * height and turn a comparison between systems into a comparison within them,
 * which is what the per-run figures are for; a row here is a voting system and
 * nothing else.
 *
 * The per-slate rows, pooled row included, are built in report_generator, and
 * the focal group's reproduce the focal table exactly -- per model and pooled --
 * so this has one code path rather than a focal case and a slate case that could
 * drift apart.
 *
 * Rows are keyed by series id and bubbles by (series, decile), so adding or
 * removing a system slides the remaining rows to their new positions and grows
 * or collapses only the bubbles that actually changed.
 */

import {
  shareScale, shareTicks, drawSeatAxis, makeTooltip, emptyState, clearEmptyState,
  ensure, motion,
} from './axes.js';

const MAX_AREA = 300;
const MIN_AREA = 14;
const ROW_HEIGHT = 34;
const WIDTH = 720;
const MARGIN = { top: 14, right: 18, bottom: 42, left: 232 };

/*
 * Outcomes are grouped into ten bands of the seat share rather than plotted at
 * the exact share.
 *
 * The systems on show fill different numbers of seats, so their exact shares
 * fall on different grids -- ninths for a 9 X 1 body, fifteenths for a 1 X 15 --
 * and bubbles that represent the same outcome never line up. Deciles are a grid
 * every system can be put on, so a band means the same thing in every row and
 * rows can be read down a column again.
 */
const DECILE_WIDTH = 10;
const DECILES = 100 / DECILE_WIDTH;

/** The band a share falls in; 100% closes the top band rather than opening an 11th. */
const decileOf = (pct) => Math.min(DECILES - 1, Math.floor(pct / DECILE_WIDTH));
/** Where a band's bubble sits: the middle of the band it stands for. */
const decileCentre = (index) => index * DECILE_WIDTH + DECILE_WIDTH / 2;
const decileLabel = (index) => `${index * DECILE_WIDTH}–${(index + 1) * DECILE_WIDTH}%`;

export function renderCrossRun(container, manifest, view) {
  const series = view.series;

  if (!series.length) {
    emptyState(container, 'No systems selected yet — add one above to start the comparison.');
    return;
  }
  clearEmptyState(container);

  const runBySlug = new Map(manifest.runs.map((r) => [r.slug, r]));
  const seriesById = new Map(series.map((s) => [s.id, s]));
  const palette = manifest.palette;
  const tooltip = makeTooltip();
  const slateId = view.slate;
  const modeId = view.mode;

  /** The chosen slate as that run sees it -- its own VAP share and wording. */
  const slateOf = (runSlug) =>
    (runBySlug.get(runSlug)?.slates || []).find((s) => s.id === slateId);

  const rowsFor = (s, test) => (s.records || [])
    .filter(test)
    .map((r) => ({ ...r, runSlug: s.runSlug, seriesId: s.id, seriesLabel: s.panelLabel }));

  // One row per selected system, carrying that system's record for the chosen
  // slate and voter model. Binned below, once the shares they fall in are known.
  const selected = series.flatMap((s) => rowsFor(
    s, (r) => r.slate === slateId && r.mode === modeId,
  ));

  const innerWidth = WIDTH - MARGIN.left - MARGIN.right;
  const innerHeight = series.length * ROW_HEIGHT;
  const height = MARGIN.top + innerHeight + MARGIN.bottom;

  /*
   * Share of the seats a system fills, not a count of them.
   *
   * Rows here are systems of different sizes -- 9 X 1, 3 X 5, 1 X 15 -- and a
   * seat axis makes those incomparable in both directions: three seats is a
   * third of one body and a fifth of another, and each row's proportional line
   * lands at its own place, so the figure's whole premise (a bubble in one
   * system sits above the same outcome in another) quietly fails. As a share,
   * every row is measured against its own body and the dotted line is the
   * slate's share of VAP for all of them at once.
   *
   * The axis is the full 0..100% whatever is on show, so it holds still as
   * systems are added and removed.
   */
  const x = shareScale(innerWidth);
  const ticks = shareTicks(DECILE_WIDTH);
  /** A record's outcome as a percentage of the seats its own system fills. */
  const seatsOf = (seriesId) => (seriesById.get(seriesId)?.totalSeats) || 0;
  const share = (d) => {
    const total = seatsOf(d.seriesId);
    return total ? (d.seats / total) * 100 : 0;
  };
  const y = d3.scalePoint().domain(series.map((s) => s.id)).range([0, innerHeight]).padding(0.5);

  /*
   * Sum each row's plans into its bands, and carry the seat counts that went in
   * so a bubble can still say what it is made of.
   */
  const binned = d3.rollups(
    selected,
    (rows) => ({
      ...rows[0],
      plans: d3.sum(rows, (r) => r.plans),
      seatsFrom: d3.min(rows, (r) => r.seats),
      seatsTo: d3.max(rows, (r) => r.seats),
    }),
    (r) => r.seriesId,
    (r) => decileOf(share(r)),
  ).flatMap(([seriesId, bands]) => bands.map(([decile, row]) => ({ ...row, seriesId, decile })));
  const records = binned.filter((d) => d.plans > 0);

  /*
   * Every row scaled against its own total, not against the largest row on show.
   *
   * A bubble is the share of that system's plans landing in that band, so a row
   * of 50 plans and a row of 1,000 are drawn on the same footing -- previously
   * the bigger run set the scale and everything else shrank against it, which
   * made the comparison a comparison of how much each run was simulated.
   *
   * The denominator is the row's own plans summed over every band, which is the
   * same number for every voter model (each covers all of the plans) and for the
   * pooled row (the average of totals that are all equal). So switching model
   * moves the bubbles without resizing the ones whose counts are unchanged,
   * which is what the previous per-model basis was protecting.
   */
  const rowTotals = new Map(d3.rollups(
    selected, (rows) => d3.sum(rows, (r) => r.plans), (r) => r.seriesId,
  ));
  const fractionOf = (d) => {
    const total = rowTotals.get(d.seriesId) || 0;
    return total ? d.plans / total : 0;
  };
  /*
   * The quantity is the fraction; the display range is stretched to the largest
   * fraction on show.
   *
   * Against a fixed 0..100% domain every bubble would be drawn small -- a
   * distribution spread over several bands rarely puts more than 40% of its
   * plans in any one of them -- and the differences between rows would be
   * squeezed into the bottom of the size range. Stretching keeps what matters:
   * two bands holding the same fraction of their rows' plans are still the same
   * size, whichever rows they belong to.
   */
  const maxFraction = d3.max(records, fractionOf) || 1;
  const area = d3.scaleLinear().domain([0, maxFraction]).range([MIN_AREA, MAX_AREA]).clamp(true);
  const radius = (d) => Math.sqrt(area(fractionOf(d)) / Math.PI);

  // Not ensureSvg: this one fills its container rather than being capped to a
  // multiple of its natural width like the per-run panels, and its viewBox grows
  // and shrinks with the row count. d3 interpolates the numbers inside a viewBox,
  // so the frame resizes with its contents instead of snapping while they
  // animate.
  const svg = ensure(container, 'svg', 'crossrun')
    .attr('role', 'img')
    .attr('aria-label', 'Seat outcomes across the selected electoral systems');
  const viewBox = `0 0 ${WIDTH} ${height}`;
  if (svg.attr('viewBox')) motion(svg).attr('viewBox', viewBox);
  else svg.attr('viewBox', viewBox);

  const panel = ensure(svg, 'g', 'panel')
    .attr('transform', `translate(${MARGIN.left},${MARGIN.top})`);

  // Gridlines run the full height: the point of this chart is that a bubble in
  // one system sits directly above the same share of seats in another.
  ensure(panel, 'g', 'gridlines').selectAll('line')
    .data(ticks, (d) => d)
    .join(
      (enter) => enter.append('line')
        .attr('class', 'gridline')
        .attr('x1', (d) => x(d)).attr('x2', (d) => x(d))
        .attr('y1', 0).attr('y2', innerHeight)
        .style('opacity', 0)
        .call((sel) => motion(sel).style('opacity', 1)),
      (update) => update.call((sel) => motion(sel)
        .attr('x1', (d) => x(d)).attr('x2', (d) => x(d))
        .attr('y1', 0).attr('y2', innerHeight)),
      (exit) => exit.call((sel) => motion(sel).style('opacity', 0).remove()),
    );

  // Two-line row labels: the run above, the system beneath. One line would need
  // a left margin wide enough for "Alternative Electoral Systems - 9 X 1 Top Two
  // (two-profile)" and leave the plot itself a sliver.
  ensure(panel, 'g', 'row-labels').selectAll('g.row-label')
    .data(series, (d) => d.id)
    .join(
      (enter) => {
        const g = enter.append('g')
          .attr('class', 'row-label')
          .attr('transform', (d) => `translate(-12,${y(d.id)})`)
          .style('opacity', 0);
        g.append('text').attr('class', 'row-run').attr('text-anchor', 'end').attr('dy', '-0.15em');
        g.append('text').attr('class', 'row-system').attr('text-anchor', 'end').attr('dy', '1em');
        return g.call((sel) => motion(sel).style('opacity', 1));
      },
      (update) => update.call((sel) => motion(sel)
        .attr('transform', (d) => `translate(-12,${y(d.id)})`)),
      (exit) => exit.call((sel) => motion(sel).style('opacity', 0).remove()),
    )
    .call((g) => {
      g.select('text.row-run').text((d) => d.run);
      g.select('text.row-system').text((d) => d.systemLabel);
    });

  /*
   * The proportional-representation line, drawn as one segment per row rather
   * than a single full-height rule.
   *
   * Each run has its own share of VAP. When they agree the segments abut and
   * read as one continuous dotted line; when a run with a different share is
   * added, its segment steps aside instead of the whole figure claiming a share
   * that only some of its rows have.
   */
  ensure(panel, 'g', 'reference').selectAll('line')
    .data(series, (d) => d.id)
    .join(
      (enter) => enter.append('line')
        .attr('class', 'reference-line')
        .attr('x1', refX).attr('x2', refX)
        .attr('y1', (d) => y(d.id) - ROW_HEIGHT / 2)
        .attr('y2', (d) => y(d.id) + ROW_HEIGHT / 2)
        .style('opacity', 0)
        .call((sel) => motion(sel).style('opacity', 1)),
      (update) => update.call((sel) => motion(sel)
        .attr('x1', refX).attr('x2', refX)
        .attr('y1', (d) => y(d.id) - ROW_HEIGHT / 2)
        .attr('y2', (d) => y(d.id) + ROW_HEIGHT / 2)),
      (exit) => exit.call((sel) => motion(sel).style('opacity', 0).remove()),
    );

  /*
   * Proportionality as a share is just the slate's share of VAP -- the seat
   * count it would be cancels out -- so every row of one slate puts its segment
   * at the same x and they abut into a single dotted line. Runs still carry
   * their own share, so a run measuring a different electorate steps aside
   * exactly as it did before.
   */
  function refX(d) {
    return x((slateOf(d.runSlug)?.vapShare ?? 0) * 100);
  }

  /*
   * The line's wording, per row.
   *
   * Not the run's `proportionalLabel`: that reads "20.4% (1.8 seats)" against
   * the run's own total, which is the wrong body for a row measuring one of its
   * contests, and it is the same sentence on every row of a figure whose rows
   * fill different numbers of seats. The share is the shared part; what it comes
   * to in seats is said per row, where the row's own total is known.
   */
  function _shareLabel(slate, seriesId) {
    const pct = d3.format('.1%')(slate.vapShare);
    const total = seriesId ? seatsOf(seriesId) : 0;
    return total
      ? `${slate.label} share of VAP: ${pct} (${d3.format('.1f')(slate.vapShare * total)} of ${total} seats)`
      : `${slate.label} share of VAP: ${pct}`;
  }

  function fill(d) {
    return palette[d.mode] || '#898781';
  }

  /** The seat counts a band gathered: one number, or the range it spans. */
  function _seatsIn(d) {
    return d.seatsFrom === d.seatsTo ? `${d.seatsFrom}` : `${d.seatsFrom}–${d.seatsTo}`;
  }

  // The dotted line is 1.2px wide and effectively impossible to hover. A
  // transparent segment of the same length, ten times as thick, carries the
  // tooltip instead -- the same trick drawReferenceLine uses on the per-run
  // figures, applied per row so each says its own slate's share.
  ensure(panel, 'g', 'reference-hits').selectAll('line')
    .data(series, (d) => d.id)
    .join((enter) => enter.append('line')
      .attr('stroke', 'transparent')
      .attr('stroke-width', 10)
      .style('pointer-events', 'stroke')
      .attr('x1', refX).attr('x2', refX)
      .attr('y1', (d) => y(d.id) - ROW_HEIGHT / 2)
      .attr('y2', (d) => y(d.id) + ROW_HEIGHT / 2))
    .call((sel) => motion(sel)
      .attr('x1', refX).attr('x2', refX)
      .attr('y1', (d) => y(d.id) - ROW_HEIGHT / 2)
      .attr('y2', (d) => y(d.id) + ROW_HEIGHT / 2))
    .on('mousemove', (event, d) => {
      const slate = slateOf(d.runSlug);
      tooltip.show(event, slate ? _shareLabel(slate, d.id) : '');
    })
    .on('mouseleave', () => tooltip.hide());

  ensure(panel, 'g', 'bubbles').selectAll('circle.bubble')
    .data(records, (d) => `${d.seriesId}-${d.decile}`)
    .join(
      (enter) => enter.append('circle')
        .attr('class', 'bubble')
        .attr('fill-opacity', 0.75)
        .attr('fill', fill)
        .attr('cx', (d) => x(decileCentre(d.decile)))
        .attr('cy', (d) => y(d.seriesId))
        .attr('r', 0)
        .call((sel) => motion(sel).attr('r', radius)),
      // Colour is updated here, not only on enter: a bubble is keyed by (series,
      // seat count), so changing voter model updates bubbles already on screen,
      // and a fill set only at entry left survivors wearing the previous model's
      // colour. It rides the existing transition rather than starting a second
      // one, which would cancel the first and strand the radius mid-flight.
      (update) => update.call((sel) => motion(sel)
        .attr('fill', fill)
        .attr('cx', (d) => x(decileCentre(d.decile)))
        .attr('cy', (d) => y(d.seriesId))
        .attr('r', radius)),
      // An exiting bubble's row has left the scale, so it collapses where it
      // stands rather than moving to a position that no longer exists.
      (exit) => exit.call((sel) => motion(sel).attr('r', 0).remove()),
    )
    .attr('data-mode', (d) => d.mode)
    .on('mousemove', (event, d) => {
      const slate = slateOf(d.runSlug);
      tooltip.show(
        event,
        `<strong>${d.seriesLabel}</strong><br>${d.slateLabel} seats · ${d.modeLabel}` +
        `${d.pooled ? ' (averaged across voter models)' : ''}<br>` +
        // The band is what the axis compares; the seats it stands for and the
        // plans behind it are what make the bubble concrete. The percentage is
        // of this row's own plans, which is what its size encodes.
        `${decileLabel(d.decile)} of seats — ${_seatsIn(d)} of ${seatsOf(d.seriesId)}<br>` +
        `${d3.format(',.1f')(d.plans)} plans (${d3.format('.1%')(fractionOf(d))} of this system's)` +
        (slate ? `<br>${_shareLabel(slate, d.seriesId)}` : ''),
      );
    })
    .on('mouseleave', () => tooltip.hide());

  // The one seat axis, under the whole stack. It slides down as rows are added,
  // in step with the viewBox growing beneath it.
  const axisWrap = ensure(svg, 'g', 'seat-axis-wrap');
  const axisAt = `translate(${MARGIN.left},${MARGIN.top + innerHeight + 8})`;
  if (axisWrap.attr('transform')) motion(axisWrap).attr('transform', axisAt);
  else axisWrap.attr('transform', axisAt);
  drawSeatAxis(axisWrap, x, ticks, 0, 'Share of seats won, by decile', (v) => `${v}%`);
}
